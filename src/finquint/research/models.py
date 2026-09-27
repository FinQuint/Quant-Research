from dataclasses import asdict, dataclass, field
from enum import Enum
from math import isfinite
from typing import Mapping


class TaskStatus(str, Enum):
    PLANNED = "planned"
    READY = "ready"
    RUNNING = "running"
    REVIEWING = "reviewing"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    COMPLETED = "completed"
    FAILED = "failed"


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(ch.isspace() for ch in value):
        raise ValueError(f"{name} must be a nonempty identifier without whitespace")
    return value


@dataclass(frozen=True)
class ResearchTask:
    task_id: str
    objective: str
    acceptance_criteria: tuple[str, ...]
    dependencies: tuple[str, ...] = ()
    assigned_agent: str | None = None
    requires_human_approval: bool = True
    status: TaskStatus = TaskStatus.PLANNED

    def __post_init__(self):
        _identifier(self.task_id, "task_id")
        if not self.objective.strip() or not self.acceptance_criteria or any(not value.strip() for value in self.acceptance_criteria):
            raise ValueError("objective and acceptance criteria are required")
        if len(set(self.dependencies)) != len(self.dependencies) or self.task_id in self.dependencies:
            raise ValueError("task dependencies must be unique and cannot reference the task itself")
        for dependency in self.dependencies:
            _identifier(dependency, "dependency")

    def to_dict(self) -> dict:
        result = asdict(self)
        result["status"] = self.status.value
        return result


@dataclass(frozen=True)
class ResearchPlan:
    plan_id: str
    hypothesis: str
    tasks: tuple[ResearchTask, ...]

    def __post_init__(self):
        _identifier(self.plan_id, "plan_id")
        if not self.hypothesis.strip() or not self.tasks:
            raise ValueError("hypothesis and tasks are required")
        ids = {task.task_id for task in self.tasks}
        if len(ids) != len(self.tasks):
            raise ValueError("task ids must be unique")
        if any(set(task.dependencies) - ids for task in self.tasks):
            raise ValueError("all task dependencies must belong to the plan")
        graph = {task.task_id: task.dependencies for task in self.tasks}
        visiting, visited = set(), set()

        def visit(task_id):
            if task_id in visiting:
                raise ValueError("task dependency graph contains a cycle")
            if task_id not in visited:
                visiting.add(task_id)
                for dependency in graph[task_id]:
                    visit(dependency)
                visiting.remove(task_id)
                visited.add(task_id)
        for task_id in graph:
            visit(task_id)


@dataclass(frozen=True)
class AgentResult:
    task_id: str
    agent: str
    success: bool
    changed_files: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    metrics: Mapping[str, float] = field(default_factory=dict)
    artifacts: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()

    def __post_init__(self):
        _identifier(self.task_id, "task_id")
        if not self.agent.strip() or any(not isfinite(float(value)) for value in self.metrics.values()):
            raise ValueError("agent and finite metrics are required")


@dataclass(frozen=True)
class ApprovalDecision:
    task_id: str
    reviewer: str
    approved: bool
    notes: str
    approved_commit: str | None = None

    def __post_init__(self):
        _identifier(self.task_id, "task_id")
        if not self.reviewer.strip() or not self.notes.strip():
            raise ValueError("reviewer and decision notes are required")


@dataclass(frozen=True)
class ExperimentRecord:
    experiment_id: str
    hypothesis_id: str
    code_commit: str
    dataset_versions: Mapping[str, str]
    parameters: Mapping[str, object]
    random_seed: int | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    metrics: Mapping[str, float] = field(default_factory=dict)
    status: str = "registered"

    def __post_init__(self):
        _identifier(self.experiment_id, "experiment_id")
        _identifier(self.hypothesis_id, "hypothesis_id")
        if not self.code_commit.strip() or not self.dataset_versions:
            raise ValueError("code commit and dataset versions are required")
        if any(not isfinite(float(value)) for value in self.metrics.values()):
            raise ValueError("experiment metrics must be finite")


@dataclass(frozen=True)
class RetrospectiveProposal:
    proposal_id: str
    source_task_id: str
    observation: str
    proposed_change: str
    requires_human_approval: bool = True

    def __post_init__(self):
        _identifier(self.proposal_id, "proposal_id")
        _identifier(self.source_task_id, "source_task_id")
        if not self.observation.strip() or not self.proposed_change.strip():
            raise ValueError("retrospective observation and proposed change are required")
