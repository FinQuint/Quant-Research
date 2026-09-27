import pytest

from finquint.research import ResearchPlan, ResearchTask, TaskStatus


def test_research_plan_validates_dependency_graph():
    plan = ResearchPlan("plan-1", "Rates predict bond returns", (
        ResearchTask("data", "Prepare data", ("No missing timestamps",), assigned_agent="data-agent"),
        ResearchTask("model", "Fit model", ("Out-of-sample results recorded",), dependencies=("data",)),
    ))
    assert plan.tasks[0].status == TaskStatus.PLANNED


def test_research_plan_rejects_unknown_dependency_and_cycles():
    with pytest.raises(ValueError, match="belong"):
        ResearchPlan("p", "h", (ResearchTask("a", "A", ("done",), dependencies=("missing",)),))
    with pytest.raises(ValueError, match="cycle"):
        ResearchPlan("p", "h", (
            ResearchTask("a", "A", ("done",), dependencies=("b",)),
            ResearchTask("b", "B", ("done",), dependencies=("a",)),
        ))


def test_task_requires_acceptance_criteria():
    with pytest.raises(ValueError):
        ResearchTask("task", "objective", ())
