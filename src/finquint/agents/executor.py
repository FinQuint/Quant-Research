from pathlib import Path
from typing import Mapping

from finquint.research import ResearchStore, ResearchWorkflow

from .core import AgentExecutionContext, SpecialistAgent, Validation


class AgentRegistry:
    def __init__(self):
        self._agents: dict[str, SpecialistAgent] = {}

    def register(self, agent: SpecialistAgent):
        if not isinstance(agent, SpecialistAgent):
            raise TypeError("agent must implement SpecialistAgent")
        name = agent.manifest.name
        if name in self._agents:
            raise ValueError(f"agent already registered: {name}")
        self._agents[name] = agent

    def get(self, name: str) -> SpecialistAgent:
        if name not in self._agents:
            raise KeyError(f"agent is not registered: {name}")
        return self._agents[name]


class AgentExecutor:
    """Runs ready tasks independently; approval-waiting tasks do not block peers."""

    def __init__(self, workflow: ResearchWorkflow, registry: AgentRegistry, workspace_root, *,
                 validations: Mapping[str, Validation] | None = None,
                 store: ResearchStore | None = None):
        self.workflow, self.registry = workflow, registry
        self.workspace_root = Path(workspace_root).resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.validations, self.store = dict(validations or {}), store
        self.traces: dict[str, tuple] = {}

    def run_ready(self, plan_id: str):
        results = []
        for task in self.workflow.refresh_ready(plan_id):
            if task.assigned_agent is None:
                raise ValueError(f"ready task {task.task_id} has no assigned agent")
            agent = self.registry.get(task.assigned_agent)
            context = AgentExecutionContext(agent.manifest, task, self.workspace_root / task.task_id,
                                            validations=self.validations, store=self.store)
            self.workflow.start(task.task_id, agent.manifest.name)
            try:
                agent.execute(task, context)
                result = context.result(success=True)
            except Exception as exc:
                context.capture_failure(f"execution failed: {type(exc).__name__}: {exc}")
                result = context.result(success=False)
            self.workflow.submit_result(result)
            self.traces[task.task_id] = context.trace
            results.append(result)
        return tuple(results)
