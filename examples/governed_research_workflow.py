"""Phase 10A governed research workflow example."""
from pathlib import Path
from tempfile import TemporaryDirectory

from finquint.research import (
    AgentResult, ApprovalDecision, ExperimentRecord, ResearchPlan, ResearchStore,
    ResearchTask, ResearchWorkflow, hash_artifact,
)

with TemporaryDirectory() as directory:
    root = Path(directory)
    store = ResearchStore(root / "research.sqlite")
    plan = ResearchPlan("curve-study", "Curve steepness predicts bond excess returns", (
        ResearchTask("data", "Prepare point-in-time curve data", ("Dataset hash recorded",), assigned_agent="data-agent"),
        ResearchTask("model", "Run walk-forward study", ("Out-of-sample metrics recorded",),
                     dependencies=("data",), assigned_agent="quant-agent"),
    ))
    workflow = ResearchWorkflow(store, plan)
    print("Ready:", [task.task_id for task in workflow.refresh_ready(plan.plan_id)])
    workflow.start("data", "data-agent")
    workflow.submit_result(AgentResult("data", "data-agent", True, tests=("schema passed",)))
    workflow.review("data", "model-risk-agent", passed=True, findings="Lineage verified")
    workflow.decide(ApprovalDecision("data", "human-reviewer", True, "Approved for research", "abc123"))

    store.register_experiment(ExperimentRecord(
        "exp-001", "curve-steepness", "abc123", {"curve-data": "sha256:example"},
        {"lookback": 252}, random_seed=42, environment={"python": "3.12"},
    ))
    result_path = root / "metrics.json"
    result_path.write_text('{"sharpe": 0.72}', encoding="utf-8")
    store.register_artifact(hash_artifact(result_path, "metrics-001", "exp-001", media_type="application/json"))
    print("Next ready:", [task.task_id for task in workflow.refresh_ready(plan.plan_id)])
    print("Audit events:", len(store.audit_log()))
    store.close()
