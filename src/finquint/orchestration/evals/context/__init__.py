"""Long-run context evals: the Phase 5 exit test.

Spec exit: "on long-run golden tasks, citation failures and forgotten criteria drop versus
baseline; all (tune) thresholds set from data."

Each LongRunTask is a scripted run of 30+ steps with 5+ acceptance criteria. Its evidence
overflows the input budget, and it contains the events that break long contexts: evidence
updated out of band (a re-run or provider update), output from another run, a result computed
on an old dataset version, an agent's notes that cite old versions, and results that exist
only in recent working history. One task has two sources that contradict each other; the
right outcome there is to escalate.

Two strategies answer the same final question with the same SimulatedModel:
  naive    The transcript, oldest messages folded into a running summary when it overflows
           (the "chained summaries" approach ADR-006 rejects). The summary keeps the most recent
           tagged lines that fit in SUMMARY_SHARE of the budget; evidence becomes a mention
           (ID and version, content dropped). Results at other summary sizes are recorded as
           naive_sensitivity: a larger summary forgets less.
  builder  The Phase 5 context layer: pinned task card, retrieval, budgets, health triggers
           and rebuild (ContextSession).

Both are scored on the final report: forgotten criteria (on the card, not in the report) and
citation failures (an addressed criterion with no citation, or a citation that is unknown,
superseded, deprecated or from another run).

The sweeps set the (tune) thresholds from these tasks; configs/context_policy.json must hold
the picked values. The simulated plug's attention span is an assumption, so thresholds picked
here hold for that plug; the sweep is re-run per real model plug (baselines are per model).

This package is part of the protected evaluator (ADR-005).
"""
from dataclasses import dataclass, replace
import json
import math
from pathlib import Path

from ...context.builder import CONFIG_PATH, ContextBuilder, ContextPolicy, HistoryEntry
from ...context.evidence import Evidence, InMemoryEvidenceStore
from ...context.session import ContextSession
from ...context.tags import REPORT_SCHEMA, mention_line, parse_line, parse_ref
from ...context.task_card import AcceptanceCriterion, TaskCard
from ...models.core import ModelCaller
from ...models.simulated import SimulatedModel
from ...models.socket import ModelReply, ModelRequest, Segment, SegmentRole
from ...models.tokens import TokenCounter
from ...verify.citations import check_citations

BASELINE_FORMAT = 1
BASELINE_PATH = Path(__file__).resolve().parent / "baseline.json"
PLUG = {"name": "simulated-v1", "context_window": 2048, "attention_tokens": 1400}
CALL_LOG_WORDS = 24             # tool-call log noise per step
SUMMARY_SHARE = 0.15            # naive summary size; part of the baseline's definition, not a system threshold
SUMMARY_SHARES = (0.10, 0.15, 0.25)   # sensitivity of the naive baseline, recorded alongside it
SPEC_DEFAULTS = {"utilization_threshold": 0.70, "history_last_n": 8, "max_failed_rebuilds": 2}
THRESHOLDS = (0.5, 0.6, 0.7, 0.8, 0.9)
HISTORY_SIZES = (2, 4, 8, 16)
REBUILD_LIMITS = (1, 2, 3)
# Rebuild-limit scenarios: the model gives a bad output this many times, then good ones.
# None = never recovers. A bad output may recur once after a rebuild, so 2 is recoverable.
RECOVERABLE = (1, 2)
UNRECOVERABLE = None
QUALITY = ("forgotten_criteria", "citation_failures", "wrong_status")
SYSTEM = ("You are a research agent. Reply with JSON matching the report schema. Address every "
          "acceptance criterion and cite the evidence for it by ID and version.")
INSTRUCTION = "Write the final report now: every acceptance criterion, each with its evidence."
_FILLER = ("spread curve coupon accrual settlement basis carry roll factor sample window regime "
           "volatility turnover drawdown benchmark tenor notional exposure hedge").split()


def plug():
    return SimulatedModel(PLUG["name"], context_window=PLUG["context_window"],
                          attention_tokens=PLUG["attention_tokens"])


# --- tasks -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Step:
    kind: str                 # evidence | update | foreign | note | call | checkpoint
    evidence: Evidence = None
    mention: tuple = ()       # (evidence_id, version, topic) for a note
    text: str = ""


@dataclass(frozen=True)
class LongRunTask:
    name: str
    card: TaskCard
    steps: tuple
    expect: str = "ok"        # "ok" or "escalated"
    about: str = ""

    @property
    def step_count(self):
        return sum(1 for s in self.steps if s.kind not in ("checkpoint", "update"))


class _Script:
    def __init__(self, run_id, datasets=None, words=60):
        self.run_id, self.datasets, self.words = run_id, dict(datasets or {}), words
        self.steps, self.n = [], 0

    def _content(self, topic, words=None):
        self.n += 1
        body = " ".join(_FILLER[(self.n * 7 + j) % len(_FILLER)] for j in range(words or self.words))
        return f"{topic.replace('_', ' ')} result {self.n}: {body}"

    def _dataset(self, dataset):
        if dataset is not None:
            return dataset
        return next((f"{k}@{v}" for k, v in sorted(self.datasets.items())), None)

    def evidence(self, topic, version="1", *, prefix="ev", tier="authoritative", run=..., dataset=None,
                 claims=None, kind="evidence"):
        item = Evidence(f"{prefix}-{topic}", version, topic, self._content(topic),
                        run_id=self.run_id if run is ... else run, dataset=self._dataset(dataset),
                        tier=tier, claims=claims or {})
        self.steps.append(Step(kind, item))
        return item

    def update(self, topic, version="2"):
        return self.evidence(topic, version, kind="update")

    def foreign(self, topic, other_run):
        return self.evidence(topic, prefix="fx", run=other_run, kind="foreign")

    def working(self, topic):
        return self.evidence(topic, prefix="wk", tier="working")

    def note(self, topic, version="1", prefix="ev"):
        self.steps.append(Step("note", mention=(f"{prefix}-{topic}", version, topic),
                               text=f"noted {topic.replace('_', ' ')} for the report"))

    def calls(self, count):
        for _ in range(count):
            self.n += 1
            log = " ".join(_FILLER[(self.n * 3 + j) % len(_FILLER)] for j in range(CALL_LOG_WORDS))
            self.steps.append(Step("call", text=f"query {self.n} returned rows: {log}"))

    def review(self, topics, calls=2):
        """A second pass: the agent re-checks each topic and notes it again."""
        for topic in topics:
            self.calls(calls)
            self.note(topic)

    def checkpoint(self):
        self.steps.append(Step("checkpoint"))


def _card(run_id, task_id, goal, criteria, datasets=None):
    return TaskCard(run_id, task_id, goal, tuple(AcceptanceCriterion(c, text, topic) for c, topic, text in criteria),
                    code_sha="golden", dataset_versions=datasets or {}, budget_limits={"model_calls": 8})


def bond_research():
    run = "lr-bond"
    topics = ["duration", "convexity", "dv01", "key_rate", "yield_curve", "liquidity"]
    s = _Script(run)
    for topic in topics:
        s.evidence(topic)
        s.calls(2)
        s.note(topic)
    s.review(topics)
    s.review(topics, calls=1)
    s.update("duration")                     # risk engine re-run: v2 replaces v1 out of band
    s.update("dv01")
    s.calls(6)
    s.checkpoint()
    s.working("price_check")
    s.foreign("liquidity", "lr-bond-old")    # an older run's output lands in this transcript
    s.calls(1)
    card = _card(run, "report", "Bond risk report for the 5y 5% semiannual bond", [
        ("C1", "duration", "State the modified duration from the current risk run"),
        ("C2", "convexity", "State the convexity"),
        ("C3", "dv01", "State the DV01 per 100 face"),
        ("C4", "key_rate", "Give the key rate durations"),
        ("C5", "yield_curve", "Name the yield curve used"),
        ("C6", "liquidity", "Assess liquidity for this run"),
        ("C7", "price_check", "Confirm the final price check computed at the end of this run"),
    ])
    return LongRunTask("longrun_bond_research", card, tuple(s.steps),
                       about="Out-of-band re-runs, another run's output, a result only in recent history.")


def backtest_analysis():
    run = "lr-backtest"
    datasets = {"prices": "v3"}
    topics = ["sharpe", "drawdown", "turnover", "hit_rate", "exposure"]
    s = _Script(run, datasets, words=70)
    for topic in topics:
        s.evidence(topic)
        s.calls(3)
        s.note(topic)
    s.review(topics)
    s.review(topics, calls=1)
    s.update("sharpe")
    s.calls(5)
    s.checkpoint()
    s.working("oos_window")
    s.calls(1)
    s.evidence("regime", dataset="prices@v2")   # computed on an old dataset version
    s.working("cost_model")
    s.calls(1)
    card = _card(run, "analysis", "Backtest analysis of the momentum strategy on prices v3", [
        ("C1", "sharpe", "Report the Sharpe ratio"),
        ("C2", "drawdown", "Report the maximum drawdown"),
        ("C3", "turnover", "Report annual turnover"),
        ("C4", "hit_rate", "Report the hit rate"),
        ("C5", "exposure", "Report gross exposure"),
        ("C6", "oos_window", "Confirm the frozen out-of-sample window checked in this run"),
        ("C7", "cost_model", "State the transaction cost model applied in this run"),
    ], datasets)
    return LongRunTask("longrun_backtest_analysis", card, tuple(s.steps),
                       about="Larger evidence, a result on an old dataset version, two recent-history results.")


def strategy_coding():
    run = "lr-coding"
    topics = ["signal_spec", "window_param", "risk_limits", "test_plan"]
    s = _Script(run, words=50)
    for topic in topics:
        s.evidence(topic)
        s.calls(4)
        s.note(topic)
    s.review(topics)
    s.review(topics, calls=1)
    s.update("window_param")
    s.update("risk_limits")
    s.calls(4)
    s.checkpoint()
    s.working("diff_review")
    s.calls(2)
    s.working("lint_result")
    s.calls(2)
    s.working("unit_tests")
    s.calls(1)
    card = _card(run, "change", "Prepare a reviewed change to the momentum signal window", [
        ("C1", "signal_spec", "Follow the signal specification"),
        ("C2", "window_param", "Use the current window parameter"),
        ("C3", "risk_limits", "Respect the current risk limits"),
        ("C4", "test_plan", "Follow the test plan"),
        ("C5", "diff_review", "Include the diff review from this run"),
        ("C6", "lint_result", "Include the lint result from this run"),
        ("C7", "unit_tests", "Include the unit test result from this run"),
    ])
    return LongRunTask("longrun_strategy_coding", card, tuple(s.steps),
                       about="Several updated specs; three results that exist only in recent history.")


def conflicting_sources():
    run = "lr-conflict"
    s = _Script(run)
    for topic in ["coupon_schedule", "day_count", "settlement", "price_source"]:
        s.evidence(topic)
        s.calls(3)
    s.evidence("compounding_a", run=None, claims={"yield_compounding": "nominal"})
    s.calls(4)
    s.evidence("compounding_b", run=None, claims={"yield_compounding": "continuous"})
    s.calls(8)
    card = _card(run, "conventions", "Settle the yield conventions for the bond analytics", [
        ("C1", "coupon_schedule", "State the coupon schedule"),
        ("C2", "day_count", "State the day count"),
        ("C3", "settlement", "State settlement lag"),
        ("C4", "price_source", "Name the price source"),
        ("C5", "compounding_a", "State the yield compounding convention (paper A)"),
        ("C6", "compounding_b", "State the yield compounding convention (paper B)"),
    ])
    return LongRunTask("longrun_conflicting_sources", card, tuple(s.steps), expect="escalated",
                       about="Two sources disagree on yield compounding; the run must escalate.")


TASKS = (bond_research(), backtest_analysis(), strategy_coding(), conflicting_sources())


# --- running a task -----------------------------------------------------------------------------

class FaultyModel:
    """Wraps a plug: its first `bad` replies cite evidence that does not exist (None = always)."""

    def __init__(self, inner, bad):
        self.inner, self.bad, self.replies = inner, bad, 0
        self.name, self.socket_version, self.capabilities = inner.name, inner.socket_version, inner.capabilities

    def count_tokens(self, text):
        return self.inner.count_tokens(text)

    def complete(self, request):
        reply = self.inner.complete(request)
        self.replies += 1
        if self.bad is not None and self.replies > self.bad:
            return reply
        report = json.loads(reply.text)
        for criterion in report["criteria"]:
            criterion["citations"] = [{"evidence_id": "ghost", "version": "1"}]
        return ModelReply(json.dumps(report, sort_keys=True), reply.model, reply.usage)


def _apply(step, store, history, transcript):
    """Feed one scripted step to the store, the working history and the naive transcript."""
    index = len(history)
    if step.kind == "update":
        store.put(step.evidence)
        return
    if step.kind in ("evidence", "foreign"):
        item = step.evidence
        store.put(item)
        call = HistoryEntry(index, "tool_call", f"fetch {item.topic}", item.run_id,
                            fingerprint=f"fetch:{item.evidence_id}:{item.version}", state_hash=str(index))
        output = HistoryEntry(index + 1, "tool_output", run_id=item.run_id, evidence=item)
        history += [call, output]
        transcript.append(call.render() + "\n" + output.render())
    elif step.kind == "note":
        entry = HistoryEntry(index, "note", step.text, mentions=(step.mention,))
        history.append(entry)
        transcript.append(entry.render())
    elif step.kind == "call":
        entry = HistoryEntry(index, "tool_call", step.text, fingerprint=f"call:{index}", state_hash=str(index))
        history.append(entry)
        transcript.append(entry.render())


def _score(task, status, output, store, **metrics):
    card = task.card
    forgotten = citations = 0
    if output is not None:
        addressed = {c["id"]: c for c in output.get("criteria", []) if c.get("id") in card.criterion_ids}
        forgotten = sum(1 for cid in card.criterion_ids if cid not in addressed)
        for criterion in addressed.values():
            cites = criterion.get("citations", [])
            citations += (0 if cites else 1) + len(check_citations(cites, store=store, run_id=card.run_id))
    elif task.expect != "escalated":
        forgotten = len(card.criteria)
    return {"forgotten_criteria": forgotten, "citation_failures": citations,
            "wrong_status": int(status != task.expect), **metrics}


def run_builder(task, policy, *, model=None):
    model = model or plug()
    store = InMemoryEvidenceStore()
    window = model.capabilities.context_window
    builder = ContextBuilder(policy, counter=TokenCounter(model), window_tokens=window)
    session = ContextSession(task.card, builder=builder, retriever=policy.retriever(store),
                             caller=ModelCaller(model), store=store, system=SYSTEM)
    transcript = []
    for step in task.steps:
        if step.kind == "checkpoint":
            session.checkpoint()
            continue
        before = len(session.history)
        _apply(step, store, session.history, transcript)
        for entry in session.history[before:]:   # loop detection sees every tool call
            if entry.kind == "tool_call":
                session.loops.observe(entry.fingerprint, entry.state_hash)
    result = session.ask(INSTRUCTION)
    return _score(task, result.status, result.output, store, model_calls=result.model_calls,
                  rebuilds=result.rebuilds, input_tokens=result.input_tokens)


def _summarize(lines, cap, counter):
    """Lossy summary: tagged lines only, evidence reduced to a mention, most recent kept."""
    kept = []
    for line in lines:
        tag = parse_line(line)
        if tag is None:
            continue
        if tag.kind == "evidence":
            evidence_id, version = parse_ref(tag)
            line = mention_line(evidence_id, version, tag.fields.get("topic", "unknown"), "summarized result")
        kept.append(line)
    out, used = [], 0
    for line in reversed(kept):
        used += counter.count(line + "\n")
        if used > cap:
            break
        out.append(line)
    return list(reversed(out))


def run_naive(task, policy, *, model=None, summary_share=None):
    model = model or plug()
    store = InMemoryEvidenceStore()
    window = model.capabilities.context_window
    counter = TokenCounter(model)
    budget = math.floor(window * (1 - policy.output_reserve))
    cap = math.floor(budget * (SUMMARY_SHARE if summary_share is None else summary_share))
    history, transcript = [], [task.card.render()]
    for step in task.steps:
        if step.kind != "checkpoint":
            _apply(step, store, history, transcript)
    summary = []

    def text():
        parts = [SYSTEM] + (["Summary of earlier conversation:\n" + "\n".join(summary)] if summary else [])
        return "\n\n".join(parts + transcript + [INSTRUCTION])

    while transcript and counter.count(text()) > budget:
        summary = _summarize(summary + transcript.pop(0).splitlines(), cap, counter)
    segments = [Segment(SegmentRole.SYSTEM, SYSTEM, stable=True)]
    if summary:
        segments.append(Segment(SegmentRole.HISTORY, "Summary of earlier conversation:\n" + "\n".join(summary)))
    segments += [Segment(SegmentRole.HISTORY, message) for message in transcript]
    segments.append(Segment(SegmentRole.VOLATILE, INSTRUCTION))
    request = ModelRequest(task.card.run_id, task.card.task_id, tuple(segments), REPORT_SCHEMA,
                           max_output_tokens=window - budget)
    result = ModelCaller(model).call(request)
    return _score(task, "ok", result.output, store, model_calls=result.attempts, rebuilds=0,
                  input_tokens=counter.count(request.text()))


def run_suite(policy=None, tasks=TASKS):
    policy = policy or ContextPolicy.load()
    return {task.name: {"naive": run_naive(task, policy), "builder": run_builder(task, policy)} for task in tasks}


def totals(results, strategy):
    keys = next(iter(results.values()))[strategy].keys()
    return {k: sum(r[strategy][k] for r in results.values()) for k in keys}


def naive_sensitivity(policy, tasks=TASKS):
    """Naive totals at several summary sizes, so the baseline's dependence on it is visible."""
    rows = []
    for share in SUMMARY_SHARES:
        runs = [run_naive(task, policy, summary_share=share) for task in tasks]
        row = {"summary_share": share}
        row.update({k: sum(r[k] for r in runs) for k in runs[0]})
        rows.append(row)
    return rows


# --- sweeps ------------------------------------------------------------------------------------

def _failures(metrics):
    return sum(metrics[k] for k in QUALITY)


def sweep_context(base=None, tasks=TASKS):
    """Grid over utilization_threshold x history_last_n, builder strategy, summed over tasks.
    Pick: fewest quality failures, then fewest model calls, rebuilds, input tokens; ties go to
    the value nearest the spec default."""
    base = base or ContextPolicy()
    rows = []
    for threshold in THRESHOLDS:
        for size in HISTORY_SIZES:
            policy = replace(base, utilization_threshold=threshold, history_last_n=size)
            runs = [run_builder(task, policy) for task in tasks]
            row = {"utilization_threshold": threshold, "history_last_n": size}
            row.update({k: sum(r[k] for r in runs) for k in runs[0]})
            rows.append(row)

    def key(row):
        distance = (abs(row["utilization_threshold"] - SPEC_DEFAULTS["utilization_threshold"])
                    + abs(math.log2(row["history_last_n"] / SPEC_DEFAULTS["history_last_n"])))
        return (_failures(row), row["model_calls"], row["rebuilds"], row["input_tokens"], round(distance, 9))
    best = min(rows, key=key)
    return rows, {"utilization_threshold": best["utilization_threshold"], "history_last_n": best["history_last_n"]}


def sweep_rebuilds(base=None, task=TASKS[0]):
    """For each limit: does a recoverable model escalate by mistake, does an unrecoverable one
    escalate, and how many calls are spent. Pick: no missed escalations, then fewest false
    escalations, then fewest calls; ties go to the spec default."""
    base = base or ContextPolicy()
    rows = []
    for limit in REBUILD_LIMITS:
        policy = replace(base, max_failed_rebuilds=limit)
        false_escalations = recoverable_calls = 0
        for bad in RECOVERABLE:
            run = run_builder(task, policy, model=FaultyModel(plug(), bad))
            false_escalations += run["wrong_status"]
            recoverable_calls += run["model_calls"]
        stuck = run_builder(task, policy, model=FaultyModel(plug(), UNRECOVERABLE))
        rows.append({"max_failed_rebuilds": limit, "false_escalations": false_escalations,
                     "missed_escalations": int(stuck["wrong_status"] == 0),
                     "recoverable_calls": recoverable_calls, "unrecoverable_calls": stuck["model_calls"]})
    best = min(rows, key=lambda r: (r["missed_escalations"], r["false_escalations"],
                                    r["recoverable_calls"] + r["unrecoverable_calls"],
                                    abs(r["max_failed_rebuilds"] - SPEC_DEFAULTS["max_failed_rebuilds"])))
    return rows, {"max_failed_rebuilds": best["max_failed_rebuilds"]}


def pick_policy():
    """Run both sweeps; the policy they pick, with every other field at its default."""
    context_rows, context_pick = sweep_context()
    tuned = replace(ContextPolicy(), **context_pick)
    rebuild_rows, rebuild_pick = sweep_rebuilds(tuned)
    return replace(tuned, **rebuild_pick), {"context": context_rows, "rebuilds": rebuild_rows}


# --- baseline ----------------------------------------------------------------------------------

def baseline_document(policy, sweeps, results):
    return {"format": BASELINE_FORMAT, "plug": dict(PLUG), "summary_share": SUMMARY_SHARE,
            "picked": {k: getattr(policy, k) for k in SPEC_DEFAULTS},
            "sweeps": sweeps, "tasks": results, "naive_sensitivity": naive_sensitivity(policy),
            "totals": {"naive": totals(results, "naive"), "builder": totals(results, "builder")}}


def load_baseline(path=BASELINE_PATH):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("format") != BASELINE_FORMAT:
        raise ValueError(f"unsupported context baseline format: {data.get('format')}")
    return data


def write_baseline(path=BASELINE_PATH, config_path=CONFIG_PATH):
    """Humans only: re-run the sweeps, write the baseline and the picked policy config."""
    policy, sweeps = pick_policy()
    document = baseline_document(policy, sweeps, run_suite(policy))
    Path(path).write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    config = {"format": 1, "source": "src/finquint/orchestration/evals/context (Phase 5 sweep, plug "
                                      f"{PLUG['name']})", "policy": policy.to_dict()}
    Path(config_path).write_text(json.dumps(config, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return document


__all__ = ["BASELINE_PATH", "PLUG", "TASKS", "FaultyModel", "LongRunTask", "Step", "baseline_document",
           "load_baseline", "naive_sensitivity", "pick_policy", "plug", "run_builder", "run_naive", "run_suite", "sweep_context",
           "sweep_rebuilds", "totals", "write_baseline"]
