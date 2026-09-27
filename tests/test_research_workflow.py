import pytest

from finquint.research import (
    AgentResult, ApprovalDecision, ResearchPlan, ResearchStore, ResearchTask,
    ResearchWorkflow, RetrospectiveProposal, TaskStatus,
)


def _workflow(tmp_path):
    store = ResearchStore(tmp_path / "research.sqlite")
    plan = ResearchPlan("study", "Curve steepness predicts excess returns", (
        ResearchTask("prepare", "Prepare point-in-time data", ("Dataset is hashed",), assigned_agent="data"),
        ResearchTask("estimate", "Estimate signal", ("Walk-forward result exists",),
                     dependencies=("prepare",), assigned_agent="quant"),
    ))
    return store, ResearchWorkflow(store, plan)


def test_human_gated_dependency_workflow(tmp_path):
    store, workflow = _workflow(tmp_path)
    assert [task.task_id for task in workflow.refresh_ready("study")] == ["prepare"]
    workflow.start("prepare", "data")
    workflow.submit_result(AgentResult("prepare", "data", True, tests=("schema passed",)))
    workflow.review("prepare", "reviewer", passed=True, findings="Lineage and timestamps verified")
    assert store.get_task("prepare").status == TaskStatus.AWAITING_APPROVAL
    workflow.decide(ApprovalDecision("prepare", "human", True, "Approved dataset", "abc123"))
    assert store.get_task("prepare").status == TaskStatus.COMPLETED
    assert [task.task_id for task in workflow.refresh_ready("study")] == ["estimate"]
    assert any(event["event_type"] == "human_decision_recorded" for event in store.audit_log(subject_id="prepare"))
    store.close()


def test_independent_review_is_enforced(tmp_path):
    store, workflow = _workflow(tmp_path)
    workflow.refresh_ready("study")
    workflow.start("prepare", "data")
    workflow.submit_result(AgentResult("prepare", "data", True))
    with pytest.raises(ValueError, match="own task"):
        workflow.review("prepare", "data", passed=True, findings="looks good")
    store.close()


def test_rejection_preserves_history_and_allows_revision(tmp_path):
    store, workflow = _workflow(tmp_path)
    workflow.refresh_ready("study")
    workflow.start("prepare", "data")
    workflow.submit_result(AgentResult("prepare", "data", True))
    workflow.review("prepare", "model-risk", passed=False, findings="Availability timestamps missing")
    workflow.revise("prepare", reason="Add availability timestamps")
    assert store.get_task("prepare").status == TaskStatus.READY
    transitions = [event for event in store.audit_log(subject_id="prepare") if event["event_type"] == "task_status_changed"]
    assert any(event["payload"]["to"] == "rejected" for event in transitions)
    store.close()


def test_failed_execution_can_be_revised(tmp_path):
    store, workflow = _workflow(tmp_path)
    workflow.refresh_ready("study")
    workflow.start("prepare", "data")
    workflow.submit_result(AgentResult("prepare", "data", False, warnings=("source unavailable",)))
    assert store.get_task("prepare").status == TaskStatus.FAILED
    workflow.revise("prepare", reason="Use approved fallback source")
    assert store.get_task("prepare").status == TaskStatus.READY
    store.close()


def test_retrospectives_require_human_approval(tmp_path):
    store, workflow = _workflow(tmp_path)
    proposal = RetrospectiveProposal("retro-1", "prepare", "Timestamps were ambiguous", "Require availability_time")
    workflow.propose_improvement(proposal)
    assert store.audit_log()[-1]["event_type"] == "retrospective_proposed"
    with pytest.raises(ValueError):
        workflow.propose_improvement(RetrospectiveProposal(
            "retro-2", "prepare", "x", "y", requires_human_approval=False,
        ))
    store.close()
