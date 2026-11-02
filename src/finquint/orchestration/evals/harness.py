"""Golden-task harness (spec: "Evals and observability", roadmap Phase 4).

A GoldenTask runs one scenario end to end through the real orchestration stack (planner,
supervisor gates, decide(), approvals, guarded tools, ledger, scheduler) inside a fresh,
isolated Env, and returns plain JSON facts. Deterministic Criteria grade the facts; no LLM
grades anything (ADR-001).

Scores
    score                   criteria passed / criteria total
    criteria_missed         failed criteria                       (lower is better)
    citation_failures       failed citation criteria              (lower is better)
    safety_failures         failed safety criteria                (lower is better)
    duplicate_side_effects  side-effecting tool calls repeated    (lower is better)
    untagged_spans          spans missing a required tag          (lower is better)
    human_interrupts        approval requests opened              (tracked)
    tool_calls              tool, reconcile and method calls      (tracked)
    spans                   trace spans recorded                  (tracked)

The baseline (baselines.json) is recorded by a human with the CLI and is part of the
protected evaluator. compare() reports a regression when a score drops, a criterion that
passed now fails, or a lower-is-better metric rises. Tracked metrics are reported as changes.
"""
from dataclasses import dataclass, field, replace
from hashlib import sha256
import json
from pathlib import Path
import tempfile
from typing import Callable

from finquint.autonomous import AutonomousWorkflow, MethodRegistry, RunStore
from finquint.research import ResearchStore

from ..adapters import ledger_for
from ..gate.approvals import ApprovalPolicy, ApprovalService, Requirement
from ..gate.capabilities import CapabilityAuthority
from ..gate.decide import default_policy
from ..gate.human_gate import HumanGate
from ..scheduler import Scheduler
from ..tools.guarded import GuardedTools, OutboxWorker
from ..tools.registry import SideEffect, ToolRegistry
from ..tracing import SQLiteSpanStore, Tracer
from .replay import Recorder, ReplayMiss, Trace, diff_outcomes

BASELINE_FORMAT = 1
BASELINE_PATH = Path(__file__).resolve().parent / "baselines.json"
KINDS = ("research", "analysis", "coding", "fault")
CATEGORIES = ("outcome", "citation", "safety")
REGRESSION_METRICS = ("criteria_missed", "citation_failures", "safety_failures",
                      "duplicate_side_effects", "untagged_spans")
TRACKED_METRICS = ("human_interrupts", "tool_calls", "spans")

# Eval-only keys for an isolated database that is deleted after each task.
_APPROVAL_KEY = b"golden-eval-approval-key-0000000"
_CAPABILITY_KEY = b"golden-eval-capability-key-00000"
REVIEWER = "golden-reviewer"
START = 1_000_000.0
DAY = 24 * 3600


class ManualClock:
    def __init__(self, now=START):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def eval_approval_policy():
    """Who may approve what inside the eval environment. Like configs/approval_policy.json,
    plus the proposal method "improve", and deploys BLOCK on expiry so both routes are tested."""
    return ApprovalPolicy(reviewers={REVIEWER: {"owner"}},
                          requirements={"merge_to_main": Requirement("owner"), "deploy": Requirement("owner"),
                                        "improve": Requirement("owner")},
                          request_ttl_seconds=DAY, approval_ttl_seconds=3600, on_expiry={"deploy": "block"})


class Env:
    """Everything a trusted host wires together, on one throwaway SQLite database.

    Every tool handler, reconcile check and method execution is routed through the Recorder,
    so the same task can run live, be recorded, or be replayed.
    """

    code_sha = "golden"

    def __init__(self, directory, *, recorder, gate_policy=None, task_name="task"):
        self.research = ResearchStore(Path(directory) / "research.sqlite")
        self.db = self.research.connection
        self.recorder = recorder
        self.clock = ManualClock()
        self.tracer = Tracer(trace_id=sha256(task_name.encode()).hexdigest()[:32], clock=self.clock,
                             run_id=task_name, task_id="harness", code_sha=self.code_sha, model="none")
        self.authority = CapabilityAuthority(_CAPABILITY_KEY, ttl_seconds=600, clock=self.clock)
        self.approvals = ApprovalService(self.db, secret=_APPROVAL_KEY, policy=eval_approval_policy(),
                                         authority=self.authority, clock=self.clock, audit=self.research._event)
        self.gate = HumanGate(self.approvals)
        self.gate_policy = gate_policy or default_policy()
        self.ledger = ledger_for(self.research)
        self.side_effect_tools = set()

    # --- wiring ------------------------------------------------------------------------

    def tools(self, *tools):
        """GuardedTools and an OutboxWorker over these Tool definitions, recorder-wrapped."""
        registry = ToolRegistry()
        for tool in tools:
            if tool.side_effect is not SideEffect.READ_ONLY:
                self.side_effect_tools.add(tool.name)
            registry.register(replace(
                tool, handler=self.recorder.wrap("tool", tool.name, tool.handler),
                reconcile=None if tool.reconcile is None else self.recorder.wrap("reconcile", tool.name, tool.reconcile)))
        guarded = GuardedTools(registry, self.ledger, self.authority, tracer=self.tracer)
        return guarded, OutboxWorker(registry, self.ledger, tracer=self.tracer)

    def scheduler(self, tools, status_for, *, run_id, preconditions=None, evidence_for=None):
        return Scheduler(self.db, run_id=run_id, policy=self.gate_policy, tools=tools, authority=self.authority,
                         gate=self.gate, status_for=status_for, preconditions=preconditions,
                         evidence_for=evidence_for, audit=self.research._event, tracer=self.tracer)

    def registry(self, *methods):
        registry = MethodRegistry()
        for method in methods:
            registry.register(replace(method, execute=self.recorder.wrap("method", method.name, method.execute)))
        return registry

    def workflow(self, registry, *, approvals=False):
        """One worker thread, so audit events come in a fixed order."""
        return AutonomousWorkflow(RunStore(self.research), registry, max_workers=1,
                                  approvals=self.gate if approvals else None, tracer=self.tracer)

    def approve(self, request_id, decision="approve", notes=""):
        return self.approvals.decide(request_id, reviewer=REVIEWER, decision=decision, notes=notes)

    # --- observation -------------------------------------------------------------------

    def event_types(self):
        return [event["event_type"] for event in self.research.audit_log()]

    def human_interrupts(self):
        return self.db.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0]

    def close(self):
        SQLiteSpanStore(self.db).save(self.tracer.spans)
        self.research.close()


@dataclass(frozen=True)
class Criterion:
    name: str
    check: Callable   # facts -> bool
    category: str = "outcome"

    def __post_init__(self):
        if self.category not in CATEGORIES:
            raise ValueError(f"category must be one of {CATEGORIES}")

    def passed(self, facts):
        try:
            return self.check(facts) is True
        except Exception:
            return False  # a fact the check needs is missing: fail, never crash the suite


@dataclass(frozen=True)
class GoldenTask:
    """known_gap: why this task is expected to score below 1.0 today (an honest baseline)."""
    name: str
    kind: str
    goal: str
    run: Callable     # Env -> facts (JSON dict)
    criteria: tuple
    known_gap: str = ""

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        names = [c.name for c in self.criteria]
        if not names or len(set(names)) != len(names):
            raise ValueError("a golden task needs uniquely named criteria")


@dataclass(frozen=True)
class TaskScore:
    task: str
    kind: str
    score: float
    criteria: dict
    metrics: dict
    known_gap: str = ""

    def to_dict(self):
        return {"kind": self.kind, "score": self.score, "criteria": dict(self.criteria),
                "metrics": dict(self.metrics), "known_gap": self.known_gap}

    @classmethod
    def from_dict(cls, task, data):
        return cls(task, data["kind"], data["score"], dict(data["criteria"]), dict(data["metrics"]),
                   data.get("known_gap", ""))


@dataclass(frozen=True)
class TaskRun:
    score: TaskScore
    outcome: dict
    calls: tuple
    divergences: tuple = ()
    spans: tuple = field(default=(), repr=False)

    @property
    def trace(self):
        return Trace(self.score.task, self.calls, self.outcome)


def _score(task, facts, env):
    criteria = {c.name: c.passed(facts) for c in task.criteria}
    failed = [c for c in task.criteria if not criteria[c.name]]
    recorder = env.recorder
    metrics = {
        "criteria_missed": len(failed),
        "citation_failures": sum(1 for c in failed if c.category == "citation"),
        "safety_failures": sum(1 for c in failed if c.category == "safety"),
        "duplicate_side_effects": sum(recorder.duplicates("tool", name) for name in sorted(env.side_effect_tools)),
        "untagged_spans": len(env.tracer.untagged()),
        "human_interrupts": env.human_interrupts(),
        "tool_calls": sum(recorder.counts.values()),
        "spans": len(env.tracer.spans),
    }
    return TaskScore(task.name, task.kind, round(sum(criteria.values()) / len(criteria), 6), criteria,
                     metrics, task.known_gap)


def run_task(task, *, mode="live", trace=None, gate_policy=None):
    """Run one golden task in a fresh environment. mode: live, record or replay (needs trace)."""
    if mode == "replay" and trace is None:
        raise ValueError("replay needs a recorded trace")
    recorder = Recorder(mode, trace.calls if trace else ())
    with tempfile.TemporaryDirectory(prefix="finquint-golden-") as directory:
        env = Env(directory, recorder=recorder, gate_policy=gate_policy, task_name=task.name)
        try:
            try:
                facts = task.run(env)
            except ReplayMiss as exc:
                facts = {"replay_miss": str(exc)}
            except Exception as exc:  # a crashing task scores zero; the suite keeps going
                facts = {"task_error": f"{type(exc).__name__}: {exc}"}
            facts = json.loads(json.dumps(facts, sort_keys=True))
            score = _score(task, facts, env)
            outcome = {"facts": facts, "events": env.event_types()}
            spans = env.tracer.spans
        finally:
            env.close()
    divergences = ()
    if mode == "replay":
        found = list(recorder.misses)
        unused = recorder.unused()
        if unused:
            found.append(f"{len(unused)} recorded call(s) were never made: "
                         + ", ".join(sorted({f"{c['kind']} {c['name']}" for c in unused})))
        found += diff_outcomes(trace.outcome, outcome)
        divergences = tuple(found)
    return TaskRun(score, outcome, tuple(recorder.calls), divergences, spans)


def replay_task(task, trace, *, gate_policy=None):
    return run_task(task, mode="replay", trace=trace, gate_policy=gate_policy)


def run_suite(tasks, *, mode="live"):
    return {task.name: run_task(task, mode=mode) for task in tasks}


# --- baselines -------------------------------------------------------------------------

def baseline_document(scores):
    return {"format": BASELINE_FORMAT,
            "tasks": {name: score.to_dict() for name, score in sorted(scores.items())}}


def save_baseline(scores, path=BASELINE_PATH):
    Path(path).write_text(json.dumps(baseline_document(scores), indent=1, sort_keys=True) + "\n",
                          encoding="utf-8")


def load_baseline(path=BASELINE_PATH):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("format") != BASELINE_FORMAT:
        raise ValueError(f"unsupported baseline format: {data.get('format')}")
    return {name: TaskScore.from_dict(name, item) for name, item in data["tasks"].items()}


@dataclass(frozen=True)
class Comparison:
    regressions: tuple
    improvements: tuple
    changes: tuple

    @property
    def ok(self):
        return not self.regressions


def compare(current, baseline):
    """current, baseline: {task name: TaskScore}."""
    regressions, improvements, changes = [], [], []
    for name in sorted(set(current) | set(baseline)):
        if name not in baseline:
            regressions.append(f"{name}: no baseline recorded")
            continue
        if name not in current:
            regressions.append(f"{name}: golden task missing from the suite")
            continue
        now, then = current[name], baseline[name]
        if now.score < then.score:
            regressions.append(f"{name}: score {then.score} -> {now.score}")
        elif now.score > then.score:
            improvements.append(f"{name}: score {then.score} -> {now.score}")
        for criterion, passed in sorted(then.criteria.items()):
            if criterion not in now.criteria:
                regressions.append(f"{name}: criterion {criterion} removed")
            elif passed and not now.criteria[criterion]:
                regressions.append(f"{name}: criterion {criterion} now fails")
            elif not passed and now.criteria[criterion]:
                improvements.append(f"{name}: criterion {criterion} now passes")
        for criterion in sorted(set(now.criteria) - set(then.criteria)):
            changes.append(f"{name}: new criterion {criterion}")
        for metric in REGRESSION_METRICS:
            old, new = then.metrics.get(metric, 0), now.metrics.get(metric, 0)
            if new > old:
                regressions.append(f"{name}: {metric} {old} -> {new}")
            elif new < old:
                improvements.append(f"{name}: {metric} {old} -> {new}")
        for metric in TRACKED_METRICS:
            old, new = then.metrics.get(metric), now.metrics.get(metric)
            if old != new:
                changes.append(f"{name}: {metric} {old} -> {new}")
    return Comparison(tuple(regressions), tuple(improvements), tuple(changes))
