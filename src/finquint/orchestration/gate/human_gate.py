"""Human gate node (spec: "Approval path", steps 1, 4-5).

open() only builds the request: a graph engine may re-run the node on resume, and the
approval service returns the same pending request for the same action.

resume() accepts exactly {"approval_record_id": ...}. A bare "approve" (or any other
payload) is refused, so nothing but a signed record can let an action through.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from .approvals import ApprovalOutcome, ApprovalService
from .decide import GateDecision, ProposedAction


class Route(str, Enum):
    EXECUTE = "execute"     # verified approval; token attached
    WAIT = "wait"           # still waiting (more approvals needed)
    REPLAN = "replan"       # reviewer asked for a revision
    BLOCK = "block"         # rejected, invalid, or expired with a BLOCK policy
    ESCALATE = "escalate"   # expired with an ESCALATE policy


@dataclass(frozen=True)
class GateResult:
    route: Route
    reason: str
    request_id: str | None = None
    token: object = None
    notes: str = ""


_ROUTES = {ApprovalOutcome.APPROVED: Route.EXECUTE, ApprovalOutcome.PENDING: Route.WAIT,
           ApprovalOutcome.REVISE: Route.REPLAN, ApprovalOutcome.REJECTED: Route.BLOCK,
           ApprovalOutcome.INVALID: Route.BLOCK}


def expiry_route(decision):
    return Route.BLOCK if decision is GateDecision.BLOCK else Route.ESCALATE


class HumanGate:
    def __init__(self, approvals):
        if not isinstance(approvals, ApprovalService):
            raise TypeError("expected ApprovalService")
        self.approvals = approvals

    def open(self, run_id, branch_id, action, *, reason, evidence=None):
        """The reviewer will see: type, target, diff, evidence, fingerprint and reason."""
        return self.approvals.request(run_id, branch_id, action, reason=reason, evidence=evidence)

    def resume(self, action, payload):
        if not isinstance(action, ProposedAction):
            raise TypeError("expected ProposedAction")
        if not isinstance(payload, Mapping) or set(payload) != {"approval_record_id"}:
            raise ValueError('resume takes only {"approval_record_id": ...}')
        result = self.approvals.verify(payload["approval_record_id"], action)
        request_id = result.request.request_id if result.request else None
        if result.outcome is ApprovalOutcome.EXPIRED:
            route = expiry_route(self.approvals.policy.expiry_decision(action.action_type))
        else:
            route = _ROUTES[result.outcome]
        return GateResult(route, result.reason, request_id, result.token, result.notes)

    def expire_stale(self):
        """(request, Route) for each request that expired unanswered."""
        return tuple((request, expiry_route(decision)) for request, decision in self.approvals.expire_stale())
