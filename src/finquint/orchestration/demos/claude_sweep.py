"""Re-run the Phase 5 context sweep with a real model, record it, and compare its picks with the
simulated plug's (configs/context_policy.json).

    # exact token counts from the API instead of the chars/4 estimate (counts are recorded too)
    python -m finquint.orchestration.demos.claude_sweep --record --exact-tokens --max-usd 3
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
from hashlib import sha256
import json
import math
from pathlib import Path
import sys

from ..context.builder import ContextPolicy
from ..evals import context as cx
from ..models.replay import ReplayMiss, ReplayPlug
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


class CountRecorder(_Wrapper):
    """Keeps every exact token count by text digest, so a replay counts exactly as the live run
    did (requests are built from counts, so their digests depend on them) without the API."""

    def __init__(self, inner):
        super().__init__(inner)
        self.counts = {}

    def count_tokens(self, text):
        key = sha256(text.encode("utf-8")).hexdigest()
        if key not in self.counts:
            self.counts[key] = self.inner.count_tokens(text)
        return self.counts[key]

    def complete(self, request):
        return self.inner.complete(request)


def recorded_counter(counts):
    """count_tokens for a replay; a text the live run never counted raises KeyError (fail loudly)."""
    return lambda text: counts[sha256(text.encode("utf-8")).hexdigest()]


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


def run_all(model, overrides=None):
    """Both sweeps, then the naive-vs-builder suite and naive sensitivity at the picked policy,
    as evals.context.write_baseline does for the simulated plug. overrides: ContextPolicy fields
    held fixed through the sweep (e.g. {"prioritize_uncited": True})."""
    base = replace(ContextPolicy(), **(overrides or {}))
    context_rows, context_pick = sweep_context(model, base)
    tuned = replace(base, **context_pick)
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

REBUILD = {"prioritize_uncited": True, "escalate_identical_rebuild": True}   # the opt-in rebuild switches
# Claude tokens per simulated-plug token, measured from the exact-count recording (2026-11-15):
# token-weighted over 4796 texts. A window scaled by it keeps the overflow the sweep was designed for.
CLAUDE_TOKEN_RATIO = 1.437
SCALED_WINDOW = round(WINDOW * CLAUDE_TOKEN_RATIO)   # 2943


def variant(*, exact=False, window=WINDOW, rebuild=False):
    return (NAME + ("_exact" if exact else "") + (f"_w{window}" if window != WINDOW else "")
            + ("_rebuild" if rebuild else ""))


def paths(model, root=RECORDINGS, *, exact=False, window=WINDOW, rebuild=False):
    base = Path(root) / model / (variant(exact=exact, window=window, rebuild=rebuild) + ".json")
    return base, base.with_suffix(".keys.json"), base.with_suffix(".scores.json")


def counts_path(model, root=RECORDINGS, *, window=WINDOW, rebuild=False):
    return paths(model, root, exact=True, window=window, rebuild=rebuild)[0].with_suffix(".counts.json")


class CacheFirst(_Wrapper):
    """Serves a request from an earlier recording when it holds one, else calls the live plug.
    A sweep that differs from a recorded one only in its rebuilds pays only for the new requests."""

    def __init__(self, live, cache):
        super().__init__(live)
        self.cache, self.hits = cache, 0

    def complete(self, request):
        try:
            reply = self.cache.complete(request)
        except ReplayMiss:
            return self.inner.complete(request)
        self.hits += 1
        return reply


def _load_replay(model, root, *, exact, window, rebuild, count_tokens=None):
    if exact and count_tokens is None:
        count_tokens = recorded_counter(json.loads(
            counts_path(model, root, window=window, rebuild=rebuild).read_text(encoding="utf-8")))
    recording, keys, _ = paths(model, root, exact=exact, window=window, rebuild=rebuild)
    base = ReplayPlug.load(recording, count_tokens=count_tokens)
    return TolerantReplay(base, json.loads(keys.read_text(encoding="utf-8")))


def record(model, *, max_usd=DEFAULT_MAX_USD, plug=None, root=RECORDINGS, exact=False, window=WINDOW,
           rebuild=False, cache=None, **plug_options):
    """plug: the model to record (default: ClaudePlug at `window`; tests pass another).
    exact: count tokens with the API (message overhead measured and subtracted) and record them.
    rebuild: run with the opt-in rebuild switches (REBUILD) held on through the sweep.
    cache: {"exact", "window", "rebuild"} of an earlier recording to serve identical requests
    from (and seed token counts from), so only new requests are paid for."""
    if plug is None:
        from ..models.claude import ClaudePlug   # optional install
        if exact:
            plug_options = {"exact_tokens": True, "message_overhead": None, **plug_options}
        plug = ClaudePlug(model, context_window=window, **plug_options)
    seed = {}
    if cache is not None and cache.get("exact") and exact:
        seed = json.loads(counts_path(model, root, window=cache["window"], rebuild=cache["rebuild"])
                          .read_text(encoding="utf-8"))
    counter = CountRecorder(plug) if exact else None
    if counter is not None:
        counter.counts.update(seed)
    budget = BudgetedPlug(counter or plug, model=model, max_usd=max_usd)
    cached = CacheFirst(budget, _load_replay(model, root, **cache)) if cache is not None else None
    recorder = KeyedRecorder(cached or budget)
    memo = MemoPlug(recorder)
    recording, keys, scores_path = paths(model, root, exact=exact, window=window, rebuild=rebuild)
    recording.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = run_all(memo, REBUILD if rebuild else None)
        complete = True
    except BudgetExceeded as exc:
        result, complete = {"stopped": str(exc)}, False
    finally:   # keep what was paid for, even on a stop or crash
        recorder.save(recording)
        keys.write_text(json.dumps(recorder.keys, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        if counter is not None:
            counts_path(model, root, window=window, rebuild=rebuild).write_text(
                json.dumps(counter.counts, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    result["run"] = {"complete": complete, "api_calls": budget.calls, "memo_hits": memo.hits,
                     "cache_hits": cached.hits if cached else 0, "usd": round(budget.spent, 4),
                     "max_usd": max_usd, "window": window, "exact_tokens": exact,
                     "rebuild_switches": REBUILD if rebuild else {},
                     "token_counts": len(counter.counts) if counter else 0}
    if complete:
        result["comparison"] = compare(result)
    scores_path.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return result


def replay(model, root=RECORDINGS, *, count_tokens=None, exact=False, window=WINDOW, rebuild=False):
    """count_tokens: the recorded plug's exact counter, if it had one (ClaudePlug by default has
    none), so requests are built, and keyed, exactly as when they were recorded. exact: replay an
    exact-count recording with its recorded counts."""
    plug = _load_replay(model, root, exact=exact, window=window, rebuild=rebuild, count_tokens=count_tokens)
    result = run_all(MemoPlug(plug), REBUILD if rebuild else None)
    result["comparison"] = compare(result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="claude-opus-5-5")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--record", action="store_true", help="call the real API (costs money) and save a recording")
    mode.add_argument("--simulated", action="store_true", help="run with the simulated plug (free dry run)")
    parser.add_argument("--max-usd", type=float, default=DEFAULT_MAX_USD)
    parser.add_argument("--exact-tokens", action="store_true",
                        help="count tokens with the API (free) instead of the chars/4 estimate")
    parser.add_argument("--window", type=int, default=WINDOW,
                        help=f"context window in the plug's tokens (scaled for Claude: {SCALED_WINDOW})")
    parser.add_argument("--rebuild-switches", action="store_true",
                        help="hold prioritize_uncited and escalate_identical_rebuild on")
    parser.add_argument("--cache-from-off", action="store_true",
                        help="with --rebuild-switches: serve requests already in the switches-off recording")
    args = parser.parse_args(argv)
    if args.simulated:
        result = run_all(cx.plug(), REBUILD if args.rebuild_switches else None)
        result["comparison"] = compare(result)
    elif args.record:
        cache = ({"exact": args.exact_tokens, "window": args.window, "rebuild": False}
                 if args.cache_from_off else None)
        result = record(args.model, max_usd=args.max_usd, exact=args.exact_tokens, window=args.window,
                        rebuild=args.rebuild_switches, cache=cache)
    else:
        result = replay(args.model, exact=args.exact_tokens, window=args.window, rebuild=args.rebuild_switches)
    summary = {k: result[k] for k in ("comparison", "run", "stopped") if k in result}
    json.dump(summary, sys.stdout, indent=1, sort_keys=True)
    print()
    return 0 if "stopped" not in result else 2


if __name__ == "__main__":
    raise SystemExit(main())
