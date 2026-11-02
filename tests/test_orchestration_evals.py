"""Phase 4 exit test (baseline scores recorded for every golden task) plus replay, faults,
baseline comparison, tracing and the eval CLI."""
import json
import sqlite3
import threading

import pytest

from finquint.autonomous import RetryableError
from finquint.orchestration import (
    DEFAULT_PROTECTED_PATHS, NULL_TRACER, ActionStatus, CapabilityAuthority, CheckResult, GateDecision, GatePolicy,
    GuardedTools, ProposedAction, SQLiteSpanStore, ToolRegistry, Tracer, decide, default_policy, to_otlp_json,
)
from finquint.orchestration.evals import (
    SUITE, Criterion, GoldenTask, Recorder, ReplayMiss, TaskScore, Trace, compare, load_baseline, replay_task,
    run_task, task, trace_path,
)
from finquint.orchestration.evals import __main__ as evals_cli

SPEC_FAULTS = ("api_down", "stale_index", "contradictory_sources", "crash_mid_merge", "expired_approval")


@pytest.fixture(scope="module")
def live():
    return {item.name: run_task(item) for item in SUITE}


# --- exit test: baseline scores recorded for every golden task -------------------------------

def test_exit_every_golden_task_has_a_recorded_baseline():
    baseline = load_baseline()
    assert set(baseline) == {item.name for item in SUITE}
    for item in SUITE:
        recorded = baseline[item.name]
        assert set(recorded.criteria) == {c.name for c in item.criteria}
        assert recorded.kind == item.kind and 0 <= recorded.score <= 1
        assert set(recorded.metrics) >= {"criteria_missed", "citation_failures", "human_interrupts", "tool_calls"}


def test_exit_suite_reproduces_its_baseline(live):
    comparison = compare({name: run.score for name, run in live.items()}, load_baseline())
    assert comparison.regressions == ()
    assert comparison.improvements == (), "scores improved: re-record the baseline in a reviewed PR"


def test_suite_covers_research_analysis_coding_and_every_spec_fault():
    assert {item.kind for item in SUITE} == {"research", "analysis", "coding", "fault"}
    names = [item.name for item in SUITE]
    for fault in SPEC_FAULTS:
        assert any(fault in name for name in names), fault
    assert len(set(names)) == len(names)


def test_only_known_gaps_score_below_one(live):
    for name, run in live.items():
        assert (run.score.score < 1) == bool(run.score.known_gap), (name, run.outcome["facts"])
    conflict = live["fault_contradictory_sources"].score
    assert conflict.criteria == {"both_claims_cited": True, "conflict_escalated": False,
                                 "no_report_on_unresolved_conflict": False}


def test_safety_outcomes_of_the_fault_suite(live):
    facts = {name: run.outcome["facts"] for name, run in live.items()}
    assert facts["fault_crash_mid_merge"]["duplicate_merges"] == 0
    assert facts["fault_crash_without_reconcile"]["final"] == ["in_doubt"]
    assert facts["fault_api_down_persistent"]["gate"] == "escalate"
    assert facts["fault_expired_approval"]["executed"] == 0
    assert facts["fault_stale_index"]["methods_run"] == 0
    assert all(run.score.metrics["duplicate_side_effects"] == 0 for run in live.values())


def test_every_span_carries_the_required_tags(live):
    spans = [span for run in live.values() for span in run.spans]
    assert spans and all(not span.missing_tags() for span in spans)
    names = {span.name for span in spans}
    assert {"workflow.task", "scheduler.step", "gate.decide", "tool.call", "outbox.dispatch",
            "outbox.recover", "scheduler.resume", "scheduler.expire"} <= names
    tool_call = next(s for s in spans if s.name == "tool.call" and s.attributes["tool"] == "merge_to_main")
    assert tool_call.attributes["task_id"] == "ship" and tool_call.parent_id is not None


# --- replay ----------------------------------------------------------------------------------

def test_every_recorded_trace_replays_without_divergence():
    for item in SUITE:
        run = replay_task(item, Trace.load(trace_path(item.name)))
        assert run.divergences == (), (item.name, run.divergences)


def test_replay_serves_recordings_and_never_calls_through():
    called = []

    def real(x):
        called.append(x)
        if x == 3:
            raise RetryableError("API down")
        return {"y": x}

    recorder = Recorder("record")
    wrapped = recorder.wrap("tool", "t", real)
    assert wrapped(1) == {"y": 1}
    with pytest.raises(RetryableError):
        wrapped(3)
    replay = Recorder("replay", recorder.calls)
    rewrapped = replay.wrap("tool", "t", real)
    assert rewrapped(1) == {"y": 1}
    with pytest.raises(RetryableError, match="API down"):
        rewrapped(3)
    with pytest.raises(ReplayMiss):
        rewrapped(2)
    assert called == [1, 3] and replay.misses and not replay.unused()
    assert rewrapped.__name__ == "real"  # wraps() keeps the identity a Method signature uses


def test_replay_detects_an_orchestration_change():
    item = task("merge_with_parallel_research")
    base = default_policy()
    no_merge_approval = GatePolicy(allowed_actions=base.allowed_actions,
                                   human_required={"deploy", "delete_production_data"},
                                   protected_paths=base.protected_paths)
    run = replay_task(item, Trace.load(trace_path(item.name)), gate_policy=no_merge_approval)
    text = "\n".join(run.divergences)
    assert "ship_while_waiting: 'waiting_approval' -> 'done'" in text
    assert "merges_before_approval: 0 -> 1" in text and "events differ" in text


def test_replay_reports_missing_and_unused_calls():
    item = task("bond_risk_analysis")
    trace = Trace.load(trace_path(item.name))
    extra = Trace(trace.task, trace.calls + ({"kind": "tool", "name": "ghost", "key": "0" * 64,
                                               "output": {}, "error": None},), trace.outcome)
    assert any("never made" in d and "tool ghost" in d for d in replay_task(item, extra).divergences)
    missing = Trace(trace.task, trace.calls[:1], trace.outcome)
    assert any("no recorded method call" in d for d in replay_task(item, missing).divergences)


def test_replay_requires_a_trace():
    with pytest.raises(ValueError):
        run_task(task("bond_risk_analysis"), mode="replay")
    with pytest.raises(ValueError):
        Recorder("sometimes")


# --- scoring and comparison ------------------------------------------------------------------

def score(value, criteria, **metrics):
    base = {"criteria_missed": 0, "citation_failures": 0, "safety_failures": 0, "duplicate_side_effects": 0,
            "untagged_spans": 0, "human_interrupts": 1, "tool_calls": 2, "spans": 3}
    return TaskScore("a", "coding", value, criteria, {**base, **metrics})


def test_compare_flags_regressions_improvements_and_changes():
    then = score(0.5, {"x": True, "y": False}, criteria_missed=1)
    worse = score(0.0, {"x": False, "y": False}, criteria_missed=2, duplicate_side_effects=1, human_interrupts=2)
    result = compare({"a": worse}, {"a": then})
    assert not result.ok
    assert set(result.regressions) == {"a: score 0.5 -> 0.0", "a: criterion x now fails",
                                       "a: criteria_missed 1 -> 2", "a: duplicate_side_effects 0 -> 1"}
    assert result.changes == ("a: human_interrupts 1 -> 2",)

    better = score(1.0, {"x": True, "y": True})
    result = compare({"a": better}, {"a": then})
    assert result.ok and "a: criterion y now passes" in result.improvements

    assert compare({"a": then}, {}).regressions == ("a: no baseline recorded",)
    assert compare({}, {"a": then}).regressions == ("a: golden task missing from the suite",)
    assert "a: criterion y removed" in compare({"a": score(1.0, {"x": True})}, {"a": then}).regressions


def test_a_crashing_task_scores_zero_without_stopping_the_suite():
    boom = GoldenTask("boom", "coding", "always crashes", lambda env: 1 / 0,
                      (Criterion("ran", lambda facts: facts["status"] == "ok"),))
    run = run_task(boom)
    assert run.score.score == 0 and "ZeroDivisionError" in run.outcome["facts"]["task_error"]


def test_golden_tasks_are_validated():
    criterion = Criterion("c", lambda facts: True)
    with pytest.raises(ValueError):
        GoldenTask("t", "poetry", "goal", lambda env: {}, (criterion,))
    with pytest.raises(ValueError):
        GoldenTask("t", "coding", "goal", lambda env: {}, (criterion, criterion))
    with pytest.raises(ValueError):
        GoldenTask("t", "coding", "goal", lambda env: {}, ())
    with pytest.raises(ValueError):
        Criterion("c", lambda facts: True, "vibes")
    assert not Criterion("c", lambda facts: "yes").passed({})  # only a real True passes


# --- tracing ---------------------------------------------------------------------------------

def test_spans_inherit_tags_nest_and_record_errors():
    tracer = Tracer(trace_id="f" * 32, clock=lambda: 7.0, run_id="r", code_sha="abc", model="none")
    with tracer.span("outer", task_id="t1") as outer:
        outer.set(extra=[1])
        with tracer.span("inner", tool="merge"):
            pass
        with pytest.raises(ValueError):
            with tracer.span("failing"):
                raise ValueError("boom")
    inner, failing, top = tracer.spans
    assert inner.parent_id == top.span_id and inner.attributes["task_id"] == "t1"
    assert failing.status == "error" and failing.error == "ValueError: boom"
    assert top.attributes["extra"] == "[1]" and not tracer.untagged()
    with tracer.span("orphan"):
        pass
    assert [s.name for s in tracer.untagged()] == ["orphan"]
    assert tracer.untagged()[0].missing_tags() == ("task_id",)


def test_worker_thread_spans_start_from_tracer_defaults():
    tracer = Tracer(run_id="r", code_sha="c", model="m")

    def work(i):
        with tracer.span("work", task_id=f"t{i}"):
            pass

    threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    with tracer.span("main", task_id="coordinator"):
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    work_spans = [s for s in tracer.spans if s.name == "work"]
    assert len(work_spans) == 4 and all(s.parent_id is None for s in work_spans)
    assert len({s.span_id for s in tracer.spans}) == 5


def test_spans_persist_to_sqlite_and_export_as_otlp():
    tracer = Tracer(trace_id="a" * 32, clock=lambda: 1.5, run_id="r", task_id="t", code_sha="c", model="m")
    with tracer.span("parent"):
        with pytest.raises(RuntimeError):
            with tracer.span("child", retries=2, ok=True, ratio=0.5):
                raise RuntimeError("down")
    store = SQLiteSpanStore(sqlite3.connect(":memory:"))
    store.save(tracer.spans)
    store.save(tracer.spans)  # idempotent
    assert store.load("a" * 32) == tracer.spans

    exported = to_otlp_json(tracer.spans)["resourceSpans"][0]["scopeSpans"][0]["spans"]
    child, parent = exported
    assert child["parentSpanId"] == parent["spanId"] and "parentSpanId" not in parent
    assert child["status"] == {"code": 2, "message": "RuntimeError: down"} and parent["status"] == {"code": 1}
    assert child["startTimeUnixNano"] == "1500000000" and len(child["traceId"]) == 32 and len(child["spanId"]) == 16
    values = {a["key"]: a["value"] for a in child["attributes"]}
    assert values["retries"] == {"intValue": "2"} and values["ok"] == {"boolValue": True}
    assert values["ratio"] == {"doubleValue": 0.5} and values["run_id"] == {"stringValue": "r"}


def test_tracing_is_off_by_default():
    tools = GuardedTools(ToolRegistry(), None, CapabilityAuthority(b"k" * 32, ttl_seconds=60))
    assert tools.tracer is NULL_TRACER
    with NULL_TRACER.span("anything", task_id="x") as span:
        span.set(more=1)
    assert NULL_TRACER.spans == () and NULL_TRACER.untagged() == ()


# --- protection and CLI ----------------------------------------------------------------------

def test_the_evals_package_is_a_protected_path():
    assert "src/finquint/orchestration/evals" in DEFAULT_PROTECTED_PATHS
    for path in ("src/finquint/orchestration/evals/baselines.json",
                 "src/finquint/orchestration/evals/replay/traces/fault_crash_mid_merge.json",
                 "src/finquint/orchestration/evals/golden/__init__.py"):
        patch = f"--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,1 @@\n-a\n+b\n"
        action = ProposedAction("open_pr", "pr:x", {"patch": patch})
        assert decide(action, ActionStatus(CheckResult.PASSED, 0), default_policy()) is GateDecision.BLOCK


def test_cli_scores_replays_and_records(tmp_path, capsys):
    assert evals_cli.main(["--task", "bond_risk_analysis"]) == 0
    assert "bond_risk_analysis" in capsys.readouterr().out
    assert evals_cli.main(["--replay", "--task", "fault_crash_mid_merge", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["divergences"] == {} and report["regressions"] == []

    baseline, traces = tmp_path / "baselines.json", tmp_path / "traces"
    args = ["--baseline", str(baseline), "--traces", str(traces)]
    assert evals_cli.main(["--write-baseline", "--record-traces", *args]) == 0
    assert set(json.loads(baseline.read_text())["tasks"]) == {item.name for item in SUITE}
    assert sorted(p.stem for p in traces.glob("*.json")) == sorted(item.name for item in SUITE)
    capsys.readouterr()
    assert evals_cli.main(["--replay", *args]) == 0

    doctored = json.loads(baseline.read_text())
    doctored["tasks"]["bond_risk_analysis"]["criteria"]["unheard_of"] = True
    baseline.write_text(json.dumps(doctored))
    assert evals_cli.main(["--task", "bond_risk_analysis", *args]) == 1
    assert "criterion unheard_of removed" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        evals_cli.main(["--replay", "--write-baseline"])
