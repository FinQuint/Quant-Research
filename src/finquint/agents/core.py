"""Capability-scoped local agents without arbitrary command execution."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from pathlib import Path
from typing import Callable, Mapping

from finquint.research import AgentResult, ResearchStore, ResearchTask, hash_artifact


class AgentCapability(str, Enum):
    READ_WORKSPACE = "read_workspace"
    WRITE_WORKSPACE = "write_workspace"
    RUN_VALIDATIONS = "run_validations"
    REGISTER_ARTIFACTS = "register_artifacts"


@dataclass(frozen=True)
class ExecutionBudget:
    max_steps: int = 50
    max_changed_files: int = 20
    max_artifacts: int = 10

    def __post_init__(self):
        for name, value in (("max_steps", self.max_steps), ("max_changed_files", self.max_changed_files),
                            ("max_artifacts", self.max_artifacts)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class AgentManifest:
    name: str
    role: str
    capabilities: frozenset[AgentCapability]
    budget: ExecutionBudget = ExecutionBudget()

    def __post_init__(self):
        if not self.name.strip() or not self.role.strip():
            raise ValueError("agent name and role are required")
        if any(not isinstance(value, AgentCapability) for value in self.capabilities):
            raise ValueError("manifest contains an invalid capability")


@dataclass(frozen=True)
class ValidationResult:
    name: str
    passed: bool
    details: str
    metrics: Mapping[str, float] = None

    def __post_init__(self):
        if not self.name.strip() or not self.details.strip():
            raise ValueError("validation name and details are required")
        metrics = {} if self.metrics is None else dict(self.metrics)
        if any(not isfinite(float(value)) for value in metrics.values()):
            raise ValueError("validation metrics must be finite")
        object.__setattr__(self, "metrics", metrics)


@dataclass(frozen=True)
class ExecutionTrace:
    sequence: int
    action: str
    target: str
    outcome: str


Validation = Callable[["AgentExecutionContext", ResearchTask], ValidationResult]


class AgentExecutionContext:
    def __init__(self, manifest: AgentManifest, task: ResearchTask, workspace, *,
                 validations: Mapping[str, Validation] | None = None,
                 store: ResearchStore | None = None):
        self.manifest, self.task = manifest, task
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.validations = dict(validations or {})
        self.store = store
        self._steps = 0
        self._changed_files: list[str] = []
        self._artifacts: list[str] = []
        self._tests: list[str] = []
        self._warnings: list[str] = []
        self._assumptions: list[str] = []
        self._metrics: dict[str, float] = {}
        self._trace: list[ExecutionTrace] = []

    def _require(self, capability: AgentCapability):
        if capability not in self.manifest.capabilities:
            raise PermissionError(f"agent lacks capability: {capability.value}")

    def _step(self, action: str, target: str, outcome="ok"):
        self._steps += 1
        if self._steps > self.manifest.budget.max_steps:
            raise RuntimeError("agent execution step budget exceeded")
        self._trace.append(ExecutionTrace(self._steps, action, target, outcome))

    def _path(self, relative_path) -> Path:
        candidate_input = Path(relative_path)
        if candidate_input.is_absolute():
            raise PermissionError("absolute paths are not allowed")
        candidate = (self.workspace / candidate_input).resolve()
        try:
            candidate.relative_to(self.workspace)
        except ValueError as exc:
            raise PermissionError("path escapes the agent workspace") from exc
        return candidate

    def read_text(self, relative_path, *, encoding="utf-8") -> str:
        self._require(AgentCapability.READ_WORKSPACE)
        path = self._path(relative_path)
        result = path.read_text(encoding=encoding)
        self._step("read", str(relative_path))
        return result

    def write_text(self, relative_path, content: str, *, encoding="utf-8"):
        self._require(AgentCapability.WRITE_WORKSPACE)
        path = self._path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding=encoding)
        relative = path.relative_to(self.workspace).as_posix()
        if relative not in self._changed_files:
            if len(self._changed_files) >= self.manifest.budget.max_changed_files:
                raise RuntimeError("changed-file budget exceeded")
            self._changed_files.append(relative)
        self._step("write", relative)

    def run_validation(self, name: str) -> ValidationResult:
        self._require(AgentCapability.RUN_VALIDATIONS)
        if name not in self.validations:
            raise PermissionError(f"validation is not approved: {name}")
        result = self.validations[name](self, self.task)
        if not isinstance(result, ValidationResult) or result.name != name:
            raise TypeError("validation returned an invalid result")
        self._tests.append(f"{name}: {'passed' if result.passed else 'failed'} - {result.details}")
        self._metrics.update({f"{name}.{key}": float(value) for key, value in result.metrics.items()})
        self._step("validate", name, "passed" if result.passed else "failed")
        return result

    def register_artifact(self, relative_path, artifact_id: str, experiment_id: str, *, media_type="application/octet-stream"):
        self._require(AgentCapability.REGISTER_ARTIFACTS)
        if self.store is None:
            raise RuntimeError("artifact registration requires a research store")
        if len(self._artifacts) >= self.manifest.budget.max_artifacts:
            raise RuntimeError("artifact budget exceeded")
        record = hash_artifact(self._path(relative_path), artifact_id, experiment_id, media_type=media_type)
        self.store.register_artifact(record)
        self._artifacts.append(artifact_id)
        self._step("register_artifact", artifact_id)
        return record

    def warn(self, message: str):
        if not message.strip():
            raise ValueError("warning cannot be empty")
        self._warnings.append(message)
        self._step("warning", message)

    def capture_failure(self, message: str):
        """Record an executor-caught failure even when the step budget is exhausted."""
        self._warnings.append(message)
        self._trace.append(ExecutionTrace(self._steps + 1, "failure", self.task.task_id, message))

    def assume(self, assumption: str):
        if not assumption.strip():
            raise ValueError("assumption cannot be empty")
        self._assumptions.append(assumption)
        self._step("assumption", assumption)

    @property
    def trace(self) -> tuple[ExecutionTrace, ...]:
        return tuple(self._trace)

    def result(self, *, success: bool) -> AgentResult:
        return AgentResult(self.task.task_id, self.manifest.name, success,
                           tuple(self._changed_files), tuple(self._tests), dict(self._metrics),
                           tuple(self._artifacts), tuple(self._warnings), tuple(self._assumptions))


class SpecialistAgent(ABC):
    manifest: AgentManifest

    @abstractmethod
    def execute(self, task: ResearchTask, context: AgentExecutionContext) -> None:
        """Perform bounded work through the supplied context only."""
