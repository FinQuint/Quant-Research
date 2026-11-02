"""Adapters between the PR #13 workflow (finquint.autonomous) and the orchestration layer.

They translate existing state into gate inputs and share the research audit database.
AutonomousWorkflow itself is unchanged; Phase 3 routes its approvals through the gate.
"""
from .gate.decide import ActionStatus, CheckResult, ProposedAction
from .tools.ledger import SQLiteLedger


def ledger_for(research_store):
    """Ledger on the ResearchStore connection, writing to its append-only audit log."""
    return SQLiteLedger(research_store.connection, audit=research_store._event)


def action_for_call(state, call):
    """A planned autonomous call as a gate action, bound to the plan's code version."""
    return ProposedAction(action_type=call["method"],
                          target=f"run:{state['plan']['run_id']}/task:{call['task_id']}",
                          params=call["parameters"], code_sha=state["plan"]["code_version"])


def status_for_task(task, *, max_attempts):
    """Map a task record to gate check status. Only explicit retryable errors count as infra errors.

    max_attempts is the plan's attempt budget (state["plan"]["max_attempts"]). A RetryableError
    task with no attempts left is marked exhausted, so the gate escalates whatever its own
    retry limit says. A "failed" RetryableError task is always exhausted.
    """
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    retries = max(task["attempts"] - 1, 0)
    retryable = (task.get("error") or "").startswith("RetryableError")
    if task["status"] == "succeeded":
        return ActionStatus(CheckResult.PASSED, retries)
    if task["status"] in {"pending", "failed"} and retryable:
        exhausted = task["status"] == "failed" or task["attempts"] >= max_attempts
        return ActionStatus(CheckResult.INFRA_ERROR, retries, retries_exhausted=exhausted)
    if task["status"] in {"failed", "needs_revision", "rejected"}:
        return ActionStatus(CheckResult.FAILED, retries)
    raise ValueError(f"task status {task['status']!r} has no check result yet")
