"""Re-run the Phase 5 context sweep with a real model, record it, and compare its picks with the
simulated plug's (configs/context_policy.json).

    # dry run with the simulated plug (free): checks the runner and reproduces the protected baseline
    python -m finquint.orchestration.demos.claude_sweep --simulated
    # live (costs money; needs `pip install anthropic` and ANTHROPIC_API_KEY); stops at --max-usd
    python -m finquint.orchestration.demos.claude_sweep --record --max-usd 6
    # offline, from the saved recording
    python -m finquint.orchestration.demos.claude_sweep

The protected sweep functions (evals.context.sweep_context / sweep_rebuilds) always use the
simulated plug, so this module repeats their loops with a model passed in. Each run is the
protected evaluator's own run_builder / run_naive / FaultyModel and its scoring, unchanged; the
pick rules below are copied from the protected module and a test checks they reproduce its picks.

The window stays at the simulated plug's 2048 tokens, so the evidence still overflows the budget
(at the Claude plug's default window nothing would). Thresholds picked here are for that window.

Live runs go through three wrappers, outermost first:
  BudgetedPlug  stops the run (BudgetExceeded) before a call once spend reaches the cap minus a
                per-call reserve. BudgetExceeded is a BaseException so the socket core, which
                treats any plug Exception as an outage, cannot turn it into a fallback or retry.
  MemoPlug      a request already answered in this sweep gets the same reply without a call.
                Saves money and makes the replay exact; identical prompts get one sample.
  KeyedRecorder records replies (see claude_report).
"""
import argparse
from dataclasses import replace
import json
import math
from pathlib import Path
import sys

from ..context.builder import ContextPolicy
from ..evals import context as cx
from ..models.replay import ReplayPlug
from ..models.socket import SOCKET_VERSION
from .claude_report import PRICES, RECORDINGS, KeyedRecorder, TolerantReplay

WINDOW = cx.PLUG["context_window"]
DEFAULT_MAX_USD = 6.0          # (tune) hard cap for one live sweep
CALL_RESERVE_USD = 0.05        # (tune) headroom kept for the call in flight
NAME = "context_sweep"


class BudgetExceeded(BaseException):
    """The sweep's spending cap was reached. Deliberately not an Exception (see module doc)."""


class _Wrapper:
    socket_version = SOCKET_VERSION

    def __init__(self, inner):
        self.inner = inner
        self.name, self.capabilities = inner.name, inner.capabilities

    def count_tokens(self, text):
        return self.inner.count_tokens(text)


class MemoPlug(_Wrapper):
    def __init__(self, inner):
        super().__init__(inner)
        self.memo, self.hits = {}, 0

    def complete(self, request):
        key = request.digest()
        if key in self.memo:
            self.hits += 1
            return self.memo[key]
        reply = self.inner.complete(request)
        self.memo[key] = reply
        return reply


class BudgetedPlug(_Wrapper):
    def __init__(self, inner, *, model, max_usd, reserve=CALL_RESERVE_USD):
        super().__init__(inner)
        self.price_in, self.price_out = PRICES.get(model, (0.0, 0.0))
        self.max_usd, self.reserve = max_usd, reserve
        self.spent, self.calls = 0.0, 0

    def complete(self, request):
        if self.spent + self.reserve > self.max_usd:
            raise BudgetExceeded(f"spent ${self.spent:.2f} of ${self.max_usd:.2f} after {self.calls} calls")
        reply = self.inner.complete(request)
        self.calls += 1
        self.spent += (reply.usage.input_tokens * self.price_in + reply.usage.output_tokens * self.price_out) / 1e6
        return reply


# --- the protected sweep's loops, with a model passed in ------------------------------------------

def _context_key(row):
    distance = (abs(row["utilization_threshold"] - cx.SPEC_DEFAULTS["utilization_threshold"])
                + abs(math.log2(row["history_last_n"] / cx.SPEC_DEFAULTS["history_last_n"])))
    return (cx._failures(row), row["model_calls"], row["rebuilds"], row["input_tokens"], round(distance, 9))


def _rebuild_key(row):
    return (row["missed_escalations"], row["false_escalations"], row["recoverable_calls"] + row["unrecoverable_calls"],
            abs(row["max_failed_rebuilds"] - cx.SPEC_DEFAULTS["max_failed_rebuilds"]))


def _sum(runs, head):
    row = dict(head)
    row.update({k: sum(r[k] for r in runs) for k in runs[0]})
    return row


def sweep_context(model, base=None, tasks=cx.TASKS):
    base = base or ContextPolicy()
    rows = [_sum([cx.run_builder(t, replace(base, utilization_threshold=th, history_last_n=n), model=model)
                  for t in tasks], {"utilization_threshold": th, "history_last_n": n})
            for th in cx.THRESHOLDS for n in cx.HISTORY_SIZES]
    best = min(rows, key=_context_key)
    return rows, {"utilization_threshold": best["utilization_threshold"], "history_last_n": best["history_last_n"]}


def sweep_rebuilds(model, base=None, task=cx.TASKS[0]):
    base = base or ContextPolicy()
    rows = []
    for limit in cx.REBUILD_LIMITS:
        policy = replace(base, max_failed_rebuilds=limit)
        false_escalations = recoverable_calls = 0
        for bad in cx.RECOVERABLE:
            run = cx.run_builder(task, policy, model=cx.FaultyModel(model, bad))
            false_escalations += run["wrong_status"]
            recoverable_calls += run["model_calls"]
        stuck = cx.run_builder(task, policy, model=cx.FaultyModel(model, cx.UNRECOVERABLE))
        rows.append({"max_failed_rebuilds": limit, "false_escalations": false_escalations,
                     "missed_escalations": int(stuck["wrong_status"] == 0),
                     "recoverable_calls": recoverable_calls, "unrecoverable_calls": stuck["model_calls"]})
    return rows, {"max_failed_rebuilds": min(rows, key=_rebuild_key)["max_failed_rebuilds"]}


def run_all(model):
    """Both sweeps, then the naive-vs-builder suite and naive sensitivity at the picked policy,
    as evals.context.write_baseline does for the simulated plug."""
    context_rows, context_pick = sweep_context(model)
    tuned = replace(ContextPolicy(), **context_pick)
    rebuild_rows, rebuild_pick = sweep_rebuilds(model, tuned)
    policy = replace(tuned, **rebuild_pick)
    tasks = {t.name: {"naive": cx.run_naive(t, policy, model=model), "builder": cx.run_builder(t, policy, model=model)}
             for t in cx.TASKS}
    sensitivity = [_sum([cx.run_naive(t, policy, model=model, summary_share=s) for t in cx.TASKS],
                        {"summary_share": s}) for s in cx.SUMMARY_SHARES]
    return {"picked": {k: getattr(policy, k) for k in cx.SPEC_DEFAULTS},
            "sweeps": {"context": context_rows, "rebuilds": rebuild_rows}, "tasks": tasks,
            "naive_sensitivity": sensitivity,
            "totals": {s: cx.totals(tasks, s) for s in ("naive", "builder")}}


def compare(result, baseline=None):
    """Claude's picks and totals next to the simulated plug's (the protected baseline)."""
    baseline = baseline or cx.load_baseline()
    return {"picked": {"model": result["picked"], "simulated": baseline["picked"]},
            "totals": {"model": result["totals"], "simulated": baseline["totals"]},
            "same_picks": result["picked"] == baseline["picked"]}


# --- record / replay ----------------------------------------------------------------------------

def paths(model, root=RECORDINGS):
    base = Path(root) / model / f"{NAME}.json"
    return base, base.with_suffix(".keys.json"), base.with_suffix(".scores.json")


def record(model, *, max_usd=DEFAULT_MAX_USD, plug=None, root=RECORDINGS, **plug_options):
    """plug: the model to record (default: ClaudePlug at the sweep window; tests pass another)."""
    if plug is None:
        from ..models.claude import ClaudePlug   # optional install
        plug = ClaudePlug(model, context_window=WINDOW, **plug_options)
    recorder = KeyedRecorder(plug)
    budget = BudgetedPlug(recorder, model=model, max_usd=max_usd)
    memo = MemoPlug(budget)
    recording, keys, scores_path = paths(model, root)
    recording.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = run_all(memo)
        complete = True
    except BudgetExceeded as exc:
        result, complete = {"stopped": str(exc)}, False
    finally:   # keep what was paid for, even on a stop or crash
        recorder.save(recording)
        keys.write_text(json.dumps(recorder.keys, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    result["run"] = {"complete": complete, "api_calls": budget.calls, "memo_hits": memo.hits,
                     "usd": round(budget.spent, 4), "max_usd": max_usd, "window": WINDOW}
    if complete:
        result["comparison"] = compare(result)
    scores_path.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return result


def replay(model, root=RECORDINGS, *, count_tokens=None):
    """count_tokens: the recorded plug's exact counter, if it had one (ClaudePlug by default has
    none), so requests are built, and keyed, exactly as when they were recorded."""
    recording, keys, _ = paths(model, root)
    base = ReplayPlug.load(recording, count_tokens=count_tokens)
    plug = TolerantReplay(base, json.loads(keys.read_text(encoding="utf-8")))
    result = run_all(MemoPlug(plug))
    result["comparison"] = compare(result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="claude-opus-5-5")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--record", action="store_true", help="call the real API (costs money) and save a recording")
    mode.add_argument("--simulated", action="store_true", help="run with the simulated plug (free dry run)")
    parser.add_argument("--max-usd", type=float, default=DEFAULT_MAX_USD)
    args = parser.parse_args(argv)
    if args.simulated:
        result = run_all(cx.plug())
        result["comparison"] = compare(result)
    elif args.record:
        result = record(args.model, max_usd=args.max_usd)
    else:
        result = replay(args.model)
    summary = {k: result[k] for k in ("comparison", "run", "stopped") if k in result}
    json.dump(summary, sys.stdout, indent=1, sort_keys=True)
    print()
    return 0 if "stopped" not in result else 2


if __name__ == "__main__":
    raise SystemExit(main())
