from .models import AgentResult, ApprovalDecision, ResearchPlan, RetrospectiveProposal, TaskStatus
from .store import ResearchStore


class ResearchWorkflow:
    """Dependency-aware workflow with independent review and human gates."""

    def __init__(self, store: ResearchStore, plan: ResearchPlan | None = None):
        self.store = store
        if plan is not None:
            self.store.register_plan(plan)

    def refresh_ready(self, plan_id: str):
        tasks = self.store.tasks(plan_id)
        statuses = {task.task_id: task.status for task in tasks}
        for task in tasks:
            if task.status == TaskStatus.PLANNED and all(statuses[d] == TaskStatus.COMPLETED for d in task.dependencies):
                self.store.set_status(task.task_id, TaskStatus.READY, details={"dependencies_satisfied": True})
        return tuple(task for task in self.store.tasks(plan_id) if task.status == TaskStatus.READY)

    def start(self, task_id: str, agent: str):
        task = self.store.get_task(task_id)
        if task.status != TaskStatus.READY:
            raise ValueError("only ready tasks may start")
        if task.assigned_agent is not None and task.assigned_agent != agent:
            raise ValueError("task is assigned to a different agent")
        self.store.set_status(task_id, TaskStatus.RUNNING, details={"agent": agent})

    def submit_result(self, result: AgentResult):
        task = self.store.get_task(result.task_id)
        if task.status != TaskStatus.RUNNING:
            raise ValueError("results may only be submitted for running tasks")
        if task.assigned_agent is not None and result.agent != task.assigned_agent:
            raise ValueError("result agent does not match task assignment")
        self.store.save_result(result)
        self.store.set_status(result.task_id, TaskStatus.REVIEWING if result.success else TaskStatus.FAILED,
                              details={"agent": result.agent, "success": result.success})

    def review(self, task_id: str, reviewer: str, *, passed: bool, findings: str):
        task = self.store.get_task(task_id)
        if task.status != TaskStatus.REVIEWING:
            raise ValueError("only reviewing tasks may receive review decisions")
        if task.assigned_agent == reviewer:
            raise ValueError("an implementation agent cannot review its own task")
        if not findings.strip():
            raise ValueError("review findings are required")
        if passed:
            target = TaskStatus.AWAITING_APPROVAL if task.requires_human_approval else TaskStatus.COMPLETED
        else:
            target = TaskStatus.REJECTED
        self.store.set_status(task_id, target, details={"reviewer": reviewer, "findings": findings})

    def decide(self, decision: ApprovalDecision):
        task = self.store.get_task(decision.task_id)
        if task.status != TaskStatus.AWAITING_APPROVAL:
            raise ValueError("task is not awaiting human approval")
        self.store.save_approval(decision)
        self.store.set_status(task.task_id, TaskStatus.COMPLETED if decision.approved else TaskStatus.REJECTED,
                              details={"reviewer": decision.reviewer, "notes": decision.notes,
                                       "approved_commit": decision.approved_commit})

    def revise(self, task_id: str, *, reason: str):
        if self.store.get_task(task_id).status not in (TaskStatus.REJECTED, TaskStatus.FAILED):
            raise ValueError("only rejected or failed tasks may be revised")
        if not reason.strip():
            raise ValueError("revision reason is required")
        self.store.set_status(task_id, TaskStatus.READY, details={"revision_reason": reason})

    def propose_improvement(self, proposal: RetrospectiveProposal):
        if not proposal.requires_human_approval:
            raise ValueError("governance improvements must require human approval")
        self.store.register_retrospective(proposal)
