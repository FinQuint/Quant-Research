from finquint.agents import (
    AgentCapability, AgentExecutor, AgentManifest, AgentRegistry, SpecialistAgent,
    ValidationResult,
)
from finquint.research import (
    ApprovalDecision, ResearchPlan, ResearchStore, ResearchTask, ResearchWorkflow,
    TaskStatus,
)


class WriterAgent(SpecialistAgent):
    manifest = AgentManifest("writer", "implementation", frozenset({
        AgentCapability.WRITE_WORKSPACE, AgentCapability.RUN_VALIDATIONS,
    }))

    def execute(self, task, context):
        context.write_text("result.txt", task.objective)
        result = context.run_validation("content")
        if not result.passed:
            raise RuntimeError(result.details)


class ForbiddenAgent(SpecialistAgent):
    manifest = AgentManifest("forbidden", "implementation", frozenset())

    def execute(self, task, context):
        context.write_text("result.txt", "not allowed")


def content_validation(context, task):
    path = context.workspace / "result.txt"
    return ValidationResult("content", path.read_text(encoding="utf-8") == task.objective, "content matches objective")


def test_executor_runs_ready_agents_and_submits_structured_results(tmp_path):
    store = ResearchStore(tmp_path / "research.sqlite")
    plan = ResearchPlan("plan", "Agents produce bounded evidence", (
        ResearchTask("a", "first", ("content validation",), assigned_agent="writer"),
        ResearchTask("b", "second", ("content validation",), assigned_agent="writer"),
    ))
    workflow = ResearchWorkflow(store, plan)
    registry = AgentRegistry()
    registry.register(WriterAgent())
    executor = AgentExecutor(workflow, registry, tmp_path / "agents", validations={"content": content_validation})
    results = executor.run_ready("plan")
    assert {result.task_id for result in results} == {"a", "b"}
    assert all(store.get_task(task_id).status == TaskStatus.REVIEWING for task_id in ("a", "b"))
    assert executor.traces["a"][-1].action == "validate"
    store.close()


def test_failed_capability_attempt_becomes_failed_task(tmp_path):
    store = ResearchStore(tmp_path / "research.sqlite")
    plan = ResearchPlan("plan", "Deny unauthorized writes", (
        ResearchTask("bad", "attempt", ("must be blocked",), assigned_agent="forbidden"),
    ))
    workflow = ResearchWorkflow(store, plan)
    registry = AgentRegistry()
    registry.register(ForbiddenAgent())
    result = AgentExecutor(workflow, registry, tmp_path / "agents").run_ready("plan")[0]
    assert not result.success
    assert "PermissionError" in result.warnings[0]
    assert store.get_task("bad").status == TaskStatus.FAILED
    store.close()


def test_waiting_approval_does_not_block_independent_chain(tmp_path):
    store = ResearchStore(tmp_path / "research.sqlite")
    plan = ResearchPlan("plan", "Independent work continues", (
        ResearchTask("approval", "needs human", ("done",), assigned_agent="writer"),
        ResearchTask("independent", "peer work", ("done",), assigned_agent="writer", requires_human_approval=False),
        ResearchTask("downstream", "continue peer chain", ("done",), dependencies=("independent",),
                     assigned_agent="writer", requires_human_approval=False),
    ))
    workflow = ResearchWorkflow(store, plan)
    registry = AgentRegistry()
    registry.register(WriterAgent())
    executor = AgentExecutor(workflow, registry, tmp_path / "agents", validations={"content": content_validation})
    executor.run_ready("plan")
    workflow.review("approval", "reviewer", passed=True, findings="await human")
    workflow.review("independent", "reviewer", passed=True, findings="approved automatically")
    assert store.get_task("approval").status == TaskStatus.AWAITING_APPROVAL
    assert store.get_task("independent").status == TaskStatus.COMPLETED
    results = executor.run_ready("plan")
    assert [result.task_id for result in results] == ["downstream"]
    store.close()


def test_registry_rejects_duplicates(tmp_path):
    registry = AgentRegistry()
    registry.register(WriterAgent())
    try:
        registry.register(WriterAgent())
    except ValueError as exc:
        assert "already registered" in str(exc)
    else:
        raise AssertionError("duplicate registration was accepted")
