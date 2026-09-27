"""Phase 10B controlled specialist-agent execution example."""
from tempfile import TemporaryDirectory

from finquint.agents import (
    AgentCapability, AgentExecutor, AgentManifest, AgentRegistry,
    SpecialistAgent, ValidationResult,
)
from finquint.research import ResearchPlan, ResearchStore, ResearchTask, ResearchWorkflow


class ResearchWriter(SpecialistAgent):
    manifest = AgentManifest("research-writer", "bounded implementation", frozenset({
        AgentCapability.WRITE_WORKSPACE, AgentCapability.RUN_VALIDATIONS,
    }))

    def execute(self, task, context):
        context.assume("Input data was approved before execution")
        context.write_text("proposal.md", f"# Result\n\n{task.objective}\n")
        if not context.run_validation("proposal_format").passed:
            raise RuntimeError("proposal validation failed")


def proposal_format(context, task):
    text = (context.workspace / "proposal.md").read_text(encoding="utf-8")
    return ValidationResult("proposal_format", text.startswith("# Result"), "required heading present")


with TemporaryDirectory() as directory:
    store = ResearchStore(f"{directory}/research.sqlite")
    plan = ResearchPlan("agent-demo", "Controlled agents can produce reviewable work", (
        ResearchTask("proposal", "Document a walk-forward research design",
                     ("Proposal format passes",), assigned_agent="research-writer"),
    ))
    workflow = ResearchWorkflow(store, plan)
    registry = AgentRegistry()
    registry.register(ResearchWriter())
    executor = AgentExecutor(workflow, registry, f"{directory}/workspaces",
                             validations={"proposal_format": proposal_format})
    result = executor.run_ready(plan.plan_id)[0]
    print("Agent result:", result)
    print("Task status:", store.get_task("proposal").status.value)
    print("Execution trace:", executor.traces["proposal"])
    store.close()
