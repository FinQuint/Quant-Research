"""Triage: a finished run's failure -> one recovery tier. Deterministic; no model is involved.

  completed    nothing to recover
  transient    an explicit RetryableError (or the attempt budget) ran out: blind retries already
               happened (ADR-011); a revised call does not fix an outage -> human
  forbidden    the plan or registry changed, or the run ended with no failed task -> human/BLOCK
  known_fix    a fixable failure with a human-approved rule -> apply it, no model call
  fixable      a hard gate failed, the input was rejected, or the method raised, on a method with
               a recovery rule -> repair agent
  unrepairable a fixable-looking failure on a method with no rule -> BLOCK, as before Phase 8
  multiple     more than one task failed -> human (the ladder repairs one call at a time)
"""
from dataclasses import dataclass
import re

TIERS = ("completed", "transient", "forbidden", "known_fix", "fixable", "unrepairable", "multiple")

_FAILED_GATE = re.compile(r"'name': '([^']+)', 'passed': False")


def failure_signature(task):
    """A stable key for what went wrong: the failed gate names, 'input', 'needs_revision' or the
    exception class. Lessons and known fixes are keyed by (method, signature)."""
    if task["status"] == "needs_revision":
        return "needs_revision"
    error = task.get("error") or ""
    if "hard validation gate failed:" in error:
        names = sorted(set(_FAILED_GATE.findall(error)))
        return "gate:" + ",".join(names) if names else "gate:unknown"
    if "input validation failed" in error:
        return "input"
    head = error.split(":", 1)[0].strip()
    return f"error:{head}" if head.isidentifier() else "error:unknown"


@dataclass(frozen=True)
class Triage:
    tier: str
    reason: str
    task_id: str = None
    call: dict = None
    signature: str = None
    failure: str = ""


def _is_transient(task):
    error = task.get("error") or ""
    return error.startswith("RetryableError") or error == "attempt budget exhausted"


def triage(state, policy):
    status = state["status"]
    if status == "completed":
        return Triage("completed", "run completed")
    failed = [(call, state["tasks"][call["task_id"]]) for call in state["plan"]["calls"]
              if state["tasks"][call["task_id"]]["status"] in ("failed", "needs_revision")]
    if not failed:
        return Triage("forbidden", state.get("error") or f"run ended {status} with no failed task")
    if len(failed) > 1:
        return Triage("multiple", f"{len(failed)} tasks failed: " + ", ".join(c["task_id"] for c, _ in failed))
    call, task = failed[0]
    failure = task.get("error") or task["status"]
    base = dict(task_id=call["task_id"], call=call, failure=failure)
    if _is_transient(task):
        return Triage("transient", "retry budget spent on a transient error", **base)
    signature = failure_signature(task)
    if policy.known_fix(call["method"], signature) is not None:
        return Triage("known_fix", f"known fix for {signature}", signature=signature, **base)
    if policy.rule(call["method"]) is None:
        return Triage("unrepairable", f"method {call['method']} has no recovery rule", signature=signature, **base)
    return Triage("fixable", f"{signature} on {call['method']}", signature=signature, **base)
