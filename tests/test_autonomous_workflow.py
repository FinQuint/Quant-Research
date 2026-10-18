from dataclasses import replace
import threading

import pandas as pd
import pytest

from finquint.agents import ValidationResult
from finquint.autonomous import (
    AutonomousWorkflow, Call, Method, NeedsRevision, Request, RetryableError, RunStore, Source,
)
from finquint.autonomous.adapters import default_registry, evaluation_gate, pipeline_adapter
from finquint.evaluation import DataQualityCheck, ResearchEvaluationSuite
from finquint.pipeline import PipelineStage, QuantPipeline
from finquint.research import ResearchStore


def request(run_id="run", *, attempts=2, extra=()):
    return Request(run_id, "Evaluate a synthetic bond", "test-commit", (
        Call("research", "source_review", {"source_ids": ["conventions"]}),
        Call("analytics", "bond_analytics", {
            "bond": {"face_value": 100, "coupon_rate": .05, "maturity_years": 5, "frequency": 2},
            "annual_yield": .04}), *extra,
    ), (Source("conventions", "repo://docs", "test-commit", "Yields use nominal annual compounding."),), attempts)


@pytest.fixture
def host(tmp_path):
    with ResearchStore(tmp_path / "research.sqlite") as research:
        store, registry = RunStore(research), default_registry()
        yield store, registry, AutonomousWorkflow(store, registry)


def ok(p, c, o):
    return ValidationResult("required", True, "Verified by host test gate")


def test_end_to_end_preserves_analytics_and_provenance(host):
    store, _, workflow = host
    state = workflow.submit(request())
    assert state["status"] == "ready"
    result = workflow.run("run")
    assert result["status"] == "completed"
    assert result["report"]["results"]["analytics"]["output"]["bond_price"] == pytest.approx(104.4912925)
    assert len(result["report"]["sources"][0]["sha256"]) == 64
    assert result["tasks"]["analytics"]["gates"][0]["passed"]
    assert workflow.run("run") == result
    assert any(e["event_type"] == "autonomous_task_finished" for e in store.research.audit_log())


def test_parallel_research_and_execution(host):
    _, registry, workflow = host
    barrier = threading.Barrier(2, timeout=5)
    for name in ("source_review", "bond_analytics"):
        original = registry.get(name)

        def execute(p, c, original=original):
            barrier.wait()
            return original.execute(p, c)
        # Construct a fresh host registry to keep public registration append-only.
        registry._methods[name] = replace(original, execute=execute)
    workflow.submit(request())
    assert workflow.run("run")["status"] == "completed"


def test_dependency_receives_only_validated_output(host):
    _, registry, workflow = host
    registry.register(Method("dependent", "1", "execution",
        lambda p, c: {"price": c["dependencies"]["analytics"]["bond_price"]}, lambda p: None, (ok,)))
    workflow.submit(request(extra=(Call("later", "dependent", {}, ("analytics",)),)))
    state = workflow.run("run")
    assert state["status"] == "completed"
    assert state["tasks"]["later"]["output"]["price"] > 100


@pytest.mark.parametrize("output", [
    {"findings": [{"source_id": "missing", "quote": "fiction"}]},
    {"findings": [{"source_id": "conventions", "quote": "invented"}]},
    {"findings": [{"source_id": "conventions", "quote": "Yields", "interpretation": "guaranteed profit"}]},
    {"findings": []},
])
def test_unsupported_research_blocks_report(host, output):
    _, registry, workflow = host
    registry._methods["source_review"] = replace(registry.get("source_review"), execute=lambda p, c: output)
    workflow.submit(request())
    state = workflow.run("run")
    assert state["status"] == "failed" and state["report"] is None
    assert state["tasks"]["research"]["attempts"] == 1


@pytest.mark.parametrize("output", [{}, {"nested": [float("nan")]}, {"bad": object()}])
def test_invalid_numeric_outputs_fail_closed(host, output):
    _, registry, workflow = host
    registry._methods["bond_analytics"] = replace(registry.get("bond_analytics"), execute=lambda p, c: output)
    workflow.submit(request())
    assert workflow.run("run")["status"] == "failed"


def test_hard_gate_cannot_be_overridden_by_human(host):
    _, registry, workflow = host
    registry._methods["bond_analytics"] = replace(registry.get("bond_analytics"), gates=(
        lambda p, c, o: ValidationResult("numerical", False, "Independent calculation disagrees"),))
    workflow.submit(request(extra=(Call("blocked", "bond_analytics", request().calls[1].parameters, ("analytics",)),)))
    state = workflow.run("run")
    assert state["status"] == "failed"
    assert state["tasks"]["blocked"]["attempts"] == 0
    with pytest.raises(ValueError):
        workflow.decide("run", "analytics", plan_digest=state["plan_digest"], reviewer="human", approved=True, notes="override")


def test_bounded_retry_and_successful_branch_not_replayed(host):
    _, registry, workflow = host
    original = registry.get("bond_analytics")
    calls = []

    def flaky(p, c):
        calls.append(1)
        if len(calls) == 1:
            raise RetryableError("temporary service interruption")
        return original.execute(p, c)
    registry._methods[original.name] = replace(original, execute=flaky)
    workflow.submit(request())
    state = workflow.run("run")
    assert state["status"] == "completed"
    assert state["tasks"]["analytics"]["attempts"] == 2
    assert state["tasks"]["research"]["attempts"] == 1


def test_retry_exhaustion_and_prompt_revision(host):
    _, registry, workflow = host

    def fail(p, c):
        raise RetryableError("offline")
    registry._methods["bond_analytics"] = replace(registry.get("bond_analytics"), execute=fail)
    workflow.submit(request())
    state = workflow.run("run")
    assert state["status"] == "failed" and state["tasks"]["analytics"]["attempts"] == 2

    def revise(p, c):
        raise NeedsRevision("Preset assumptions do not apply; revise the plan")
    registry._methods["bond_analytics"] = replace(registry.get("bond_analytics"), execute=revise)
    workflow.submit(request("revision"))
    state = workflow.run("revision")
    assert state["status"] == "needs_revision" and state["tasks"]["analytics"]["attempts"] == 1


def test_risk_approval_persists_across_restart(tmp_path):
    path = tmp_path / "research.sqlite"
    registry = default_registry()
    registry._methods["bond_analytics"] = replace(registry.get("bond_analytics"), requires_approval=True)
    with ResearchStore(path) as research:
        workflow = AutonomousWorkflow(RunStore(research), registry)
        workflow.submit(request())
        state = workflow.run("run")
        assert state["status"] == "awaiting_approval"
        assert state["tasks"]["research"]["status"] == "succeeded"
        assert state["tasks"]["analytics"]["attempts"] == 0
    with ResearchStore(path) as research:
        workflow = AutonomousWorkflow(RunStore(research), registry)
        with pytest.raises(ValueError):
            workflow.decide("run", "analytics", plan_digest="stale", reviewer="human", approved=True, notes="reviewed")
        workflow.decide("run", "analytics", plan_digest=state["plan_digest"], reviewer="human", approved=True, notes="Reviewed risk and inputs")
        state = workflow.run("run")
        assert state["status"] == "completed"
        assert state["tasks"]["research"]["attempts"] == 1


def test_denial_is_terminal(host):
    _, registry, workflow = host
    registry._methods["bond_analytics"] = replace(registry.get("bond_analytics"), requires_approval=True)
    workflow.submit(request())
    state = workflow.run("run")
    workflow.decide("run", "analytics", plan_digest=state["plan_digest"], reviewer="human", approved=False, notes="Risk rejected")
    assert workflow.run("run")["status"] == "rejected"


def test_method_policy_drift_requires_new_plan(host):
    _, registry, workflow = host
    workflow.submit(request())
    registry._methods["bond_analytics"] = replace(registry.get("bond_analytics"), version="2")
    assert workflow.run("run")["status"] == "needs_revision"


def test_claim_and_human_recovery_consume_budget(host):
    store, _, workflow = host
    workflow.submit(request(attempts=1))
    state, claimed = store.claim("run")
    assert claimed
    with pytest.raises(RuntimeError):
        workflow.run("run")
    state["tasks"]["analytics"].update(status="running", attempts=1)
    store.save(state, "simulated_crash")
    workflow.recover("run", reviewer="operator", notes="Old process stopped")
    state = workflow.run("run")
    assert state["status"] == "failed" and state["tasks"]["analytics"]["attempts"] == 1


def test_proposals_are_inert_and_require_approval(host, tmp_path):
    _, registry, workflow = host
    patch = "diff --git a/example.py b/example.py\n+print('proposal only')"
    registry.register(Method("improve", "1", "proposal", lambda p, c: {"patch": patch, "rationale": "Review this suggestion"},
                             lambda p: None, (ok,)))
    workflow.submit(request(extra=(Call("proposal", "improve", {}, ("analytics",)),)))
    state = workflow.run("run")
    assert state["status"] == "awaiting_approval"
    workflow.decide("run", "proposal", plan_digest=state["plan_digest"], reviewer="human", approved=True, notes="Generate a suggestion")
    state = workflow.run("run")
    assert state["tasks"]["proposal"]["output"]["patch"] == patch
    assert not (tmp_path / "example.py").exists()


def test_planner_rejects_unknown_methods_cycles_and_missing_gates(host):
    _, _, workflow = host
    with pytest.raises(ValueError):
        workflow.submit(request(extra=(Call("unknown", "arbitrary_shell", {}),)))
    with pytest.raises(ValueError):
        request(extra=(Call("cycle", "source_review", {}, ("cycle",)),))
    with pytest.raises(ValueError):
        Method("unsafe", "1", "execution", lambda p, c: {}, lambda p: None, ())
    with pytest.raises(ValueError):
        request(attempts=99)


def test_existing_evaluation_suite_adapts_to_hard_gate():
    gate = evaluation_gate("data", ResearchEvaluationSuite([DataQualityCheck()]),
                           lambda p, c, o: pd.DataFrame({"x": [1, 1]}))
    assert not gate({}, {}, {}).passed


def test_pipeline_continue_on_error_cannot_bypass_supervisor():
    class Broken(PipelineStage):
        def run(self, data, context):
            raise ValueError("stage failed")
    adapter = pipeline_adapter(lambda p: QuantPipeline(continue_on_error=True).add(Broken()))
    with pytest.raises(ValueError, match="failed stages"):
        adapter({}, {})


def test_gate_exception_is_not_retryable(host):
    _, registry, workflow = host

    def gate(p, c, o):
        raise RetryableError("validator outage must block promotion")
    registry._methods["bond_analytics"] = replace(registry.get("bond_analytics"), gates=(gate,))
    workflow.submit(request())
    state = workflow.run("run")
    assert state["status"] == "failed"
    assert state["tasks"]["analytics"]["attempts"] == 1


def test_frozen_parameters_and_sources_survive_caller_mutation(host):
    _, _, workflow = host
    req = request()
    workflow.submit(req)
    req.calls[1].parameters["annual_yield"] = .99
    req.calls[0].parameters["source_ids"].append("fiction")
    state = workflow.run("run")
    assert state["status"] == "completed"
    assert state["tasks"]["analytics"]["output"]["ytm"] == .04


def test_cross_connection_claim_prevents_duplicate_dispatch(tmp_path):
    path = tmp_path / "research.sqlite"
    with ResearchStore(path) as first, ResearchStore(path) as second:
        a, b = RunStore(first), RunStore(second)
        AutonomousWorkflow(a, default_registry()).submit(request())
        assert a.claim("run")[1]
        with pytest.raises(RuntimeError, match="already active"):
            b.claim("run")


def test_changed_gate_invalidates_plan_even_without_version_bump(host):
    _, registry, workflow = host
    workflow.submit(request())
    registry._methods["bond_analytics"] = replace(registry.get("bond_analytics"), gates=(ok,))
    assert workflow.run("run")["status"] == "needs_revision"
