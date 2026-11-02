"""Parent scheduler with branch-level pausing (spec: "Only the affected branch pauses").

Each branch is an ordered list of proposed actions with its own persisted state. tick()
advances every running branch by one action, so a branch waiting for approval never
stalls independent branches. Branches are stepped in turn by one coordinator (no threads),
which keeps runs deterministic and replayable; state survives a restart.

Per action: decide() routes; AUTO_CONTINUE actions get a token and run through
GuardedTools; REQUEST_HUMAN opens an approval request and pauses the branch until
resume() brings a signed approval record. Before an approved action runs, its checks
and preconditions are evaluated again (spec step 7).
"""
from dataclasses import dataclass
from enum import Enum
import json

from finquint.autonomous.models import canonical

from .gate.approvals import RequestStatus
from .gate.capabilities import CapabilityError
from .gate.decide import CheckResult, GateDecision, ProposedAction, decide
from .gate.human_gate import Route, expiry_route
from .tools.ledger import EffectStatus
from .tracing import NULL_TRACER
from .verify.paths import ProtectedPathError


class BranchStatus(str, Enum):
    RUNNING = "running"
    WAITING = "waiting_approval"
    NEEDS_REVISION = "needs_revision"
    BLOCKED = "blocked"
    ESCALATED = "escalated"
    DONE = "done"


ACTIVE = frozenset({BranchStatus.RUNNING})


@dataclass(frozen=True)
class BranchState:
    branch_id: str
    status: BranchStatus
    cursor: int
    actions: tuple
    request_id: str | None = None
    retry_count: int = 0
    notes: str = ""

    @property
    def current(self):
        return self.actions[self.cursor] if self.cursor < len(self.actions) else None


_ADVANCED = frozenset({EffectStatus.DONE, EffectStatus.QUEUED, EffectStatus.DISPATCHING})


class Scheduler:
    """status_for(branch_id, action, retry_count) -> ActionStatus: the action's checks now.
    preconditions(action) -> iterable of failure messages (e.g. code_sha or target moved).
    evidence_for(action) -> JSON dict shown to the reviewer (diff, test results).
    tracer: optional Tracer; each step, resume and expiry pass gets a span tagged with the
    branch as task_id."""

    def __init__(self, connection, *, run_id, policy, tools, authority, gate, status_for,
                 preconditions=None, evidence_for=None, audit=None, tracer=None):
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id is required")
        self.db, self.run_id, self.policy, self.tools = connection, run_id, policy, tools
        self.authority, self.gate, self.status_for = authority, gate, status_for
        self.preconditions, self.evidence_for, self._audit = preconditions, evidence_for, audit
        self.tracer = tracer or NULL_TRACER
        self.db.execute("""
        CREATE TABLE IF NOT EXISTS orchestration_branches (
            run_id TEXT NOT NULL, branch_id TEXT NOT NULL, status TEXT NOT NULL, cursor INTEGER NOT NULL,
            actions TEXT NOT NULL, request_id TEXT, retry_count INTEGER NOT NULL, notes TEXT NOT NULL,
            PRIMARY KEY (run_id, branch_id))""")
        self.db.commit()

    # --- persistence ---------------------------------------------------------------------

    @staticmethod
    def _state(row):
        branch_id, status, cursor, actions, request_id, retries, notes = tuple(row)
        return BranchState(branch_id, BranchStatus(status), cursor,
                           tuple(ProposedAction.from_dict(a) for a in json.loads(actions)),
                           request_id, retries, notes)

    def branch(self, branch_id):
        row = self.db.execute("SELECT branch_id,status,cursor,actions,request_id,retry_count,notes "
                              "FROM orchestration_branches WHERE run_id=? AND branch_id=?",
                              (self.run_id, branch_id)).fetchone()
        if row is None:
            raise KeyError(branch_id)
        return self._state(row)

    def branches(self):
        rows = self.db.execute("SELECT branch_id,status,cursor,actions,request_id,retry_count,notes "
                               "FROM orchestration_branches WHERE run_id=? ORDER BY rowid", (self.run_id,)).fetchall()
        return tuple(self._state(r) for r in rows)

    def _save(self, state, event):
        with self.db:
            self.db.execute("UPDATE orchestration_branches SET status=?,cursor=?,actions=?,request_id=?,"
                            "retry_count=?,notes=? WHERE run_id=? AND branch_id=?",
                            (state.status.value, state.cursor, canonical([a.to_dict() for a in state.actions]),
                             state.request_id, state.retry_count, state.notes, self.run_id, state.branch_id))
            if self._audit is not None:
                self._audit(event, self.run_id, {"branch_id": state.branch_id, "status": state.status.value,
                                                 "cursor": state.cursor, "request_id": state.request_id,
                                                 "notes": state.notes})
        return state

    def _update(self, state, event, **changes):
        fields = {**state.__dict__, **changes}
        return self._save(BranchState(**fields), event)

    def add_branch(self, branch_id, actions):
        actions = tuple(actions)
        if not isinstance(branch_id, str) or not branch_id.strip():
            raise ValueError("branch_id is required")
        if not actions or not all(isinstance(a, ProposedAction) for a in actions):
            raise ValueError("a branch needs at least one ProposedAction")
        with self.db:
            self.db.execute("INSERT INTO orchestration_branches VALUES (?,?,?,?,?,?,?,?)",
                            (self.run_id, branch_id, BranchStatus.RUNNING.value, 0,
                             canonical([a.to_dict() for a in actions]), None, 0, ""))
            if self._audit is not None:
                self._audit("branch_added", self.run_id, {"branch_id": branch_id, "actions": len(actions)})
        return self.branch(branch_id)

    # --- stepping ------------------------------------------------------------------------

    def _execute(self, state, action, token):
        failures = list(self.preconditions(action)) if self.preconditions else []
        if failures:
            return self._update(state, "branch_escalated", status=BranchStatus.ESCALATED, request_id=None,
                                notes="preconditions failed: " + "; ".join(failures))
        try:
            outcome = self.tools.call(self.run_id, action, token=token,
                                      attempt_scope=f"{state.branch_id}:{state.cursor}")
        except (CapabilityError, ProtectedPathError) as exc:
            return self._update(state, "branch_blocked", status=BranchStatus.BLOCKED, request_id=None, notes=str(exc))
        if outcome.status in _ADVANCED:
            cursor = state.cursor + 1
            status = BranchStatus.DONE if cursor >= len(state.actions) else BranchStatus.RUNNING
            return self._update(state, "branch_advanced", status=status, cursor=cursor, request_id=None,
                                retry_count=0, notes="")
        if outcome.status is EffectStatus.FAILED:
            return self._update(state, "branch_blocked", status=BranchStatus.BLOCKED, request_id=None,
                                notes=outcome.error or "tool failed")
        return self._update(state, "branch_escalated", status=BranchStatus.ESCALATED, request_id=None,
                            notes=f"side effect is {outcome.status.value}; a human must resolve it")

    def _span(self, name, state):
        action = state.current
        return self.tracer.span(name, run_id=self.run_id, task_id=state.branch_id,
                                code_sha=(action.code_sha or None) if action else None,
                                action_type=action.action_type if action else None, cursor=state.cursor)

    def _step(self, state):
        with self._span("scheduler.step", state) as span:
            result = self._step_inner(state)
            span.set(branch_status=result.status.value)
            return result

    def _step_inner(self, state):
        action = state.current
        if action is None:
            return self._update(state, "branch_done", status=BranchStatus.DONE)
        status = self.status_for(state.branch_id, action, state.retry_count)
        decision = decide(action, status, self.policy)
        with self.tracer.span("gate.decide", checks=status.checks.value, retry_count=status.retry_count) as span:
            span.set(decision=decision.value)
        if decision is GateDecision.AUTO_CONTINUE:
            return self._execute(state, action, self.authority.grant(action, decision))
        if decision is GateDecision.RETRY:
            return self._update(state, "branch_retry", retry_count=state.retry_count + 1)
        if decision is GateDecision.REQUEST_HUMAN:
            request = self.gate.open(self.run_id, state.branch_id, action,
                                     reason=f"policy requires human approval for {action.action_type}",
                                     evidence=self.evidence_for(action) if self.evidence_for else {})
            return self._update(state, "branch_waiting", status=BranchStatus.WAITING, request_id=request.request_id)
        if decision is GateDecision.ESCALATE:
            return self._update(state, "branch_escalated", status=BranchStatus.ESCALATED,
                                notes="check could not run within the retry budget")
        return self._update(state, "branch_blocked", status=BranchStatus.BLOCKED,
                            notes=f"gate blocked {action.action_type}")

    def _expire(self):
        self.gate.expire_stale()
        for state in self.branches():
            if state.status is not BranchStatus.WAITING:
                continue
            request = self.gate.approvals.get_request(state.request_id)
            if request is not None and request.status is RequestStatus.EXPIRED:
                route = expiry_route(self.gate.approvals.policy.expiry_decision(state.current.action_type))
                status = BranchStatus.BLOCKED if route is Route.BLOCK else BranchStatus.ESCALATED
                with self._span("scheduler.expire", state) as span:
                    self._update(state, f"branch_{status.value}", status=status, request_id=None,
                                 notes="approval request expired unanswered")
                    span.set(branch_status=status.value)

    def tick(self):
        """Expire stale requests, then advance every running branch by one step."""
        self._expire()
        for state in self.branches():
            if state.status in ACTIVE:
                self._step(state)
        return self.branches()

    def run_until_idle(self, *, max_ticks=100):
        for _ in range(max_ticks):
            states = self.tick()
            if not any(s.status in ACTIVE for s in states):
                return states
        return self.branches()

    # --- human entry points --------------------------------------------------------------

    def resume(self, branch_id, payload):
        """Resume a waiting branch with {"approval_record_id": ...} only."""
        state = self.branch(branch_id)
        with self._span("scheduler.resume", state) as span:
            result = self._resume(state, branch_id, payload)
            span.set(branch_status=result.status.value)
            return result

    def _resume(self, state, branch_id, payload):
        if state.status is not BranchStatus.WAITING:
            raise ValueError(f"branch {branch_id} is not waiting for approval")
        if not isinstance(payload, dict) or set(payload) != {"approval_record_id"}:
            raise ValueError('resume takes only {"approval_record_id": ...}')
        record = self.gate.approvals.get_record(payload["approval_record_id"])
        if record is None or record.request_id != state.request_id:
            raise ValueError("approval record does not belong to this branch's request")
        action = state.current
        result = self.gate.resume(action, payload)
        if result.route is Route.WAIT:
            return state
        if result.route is Route.REPLAN:
            return self._update(state, "branch_needs_revision", status=BranchStatus.NEEDS_REVISION,
                                request_id=None, notes=result.notes)
        if result.route is Route.BLOCK:
            return self._update(state, "branch_blocked", status=BranchStatus.BLOCKED, request_id=None,
                                notes=result.notes or result.reason)
        if result.route is Route.ESCALATE:
            return self._update(state, "branch_escalated", status=BranchStatus.ESCALATED, request_id=None,
                                notes=result.reason)
        # EXECUTE: the checks must still pass and the gate must still ask for exactly this approval.
        status = self.status_for(state.branch_id, action, 0)
        if status.checks is not CheckResult.PASSED or decide(action, status, self.policy) is not GateDecision.REQUEST_HUMAN:
            return self._update(state, "branch_escalated", status=BranchStatus.ESCALATED, request_id=None,
                                notes="checks or policy changed after approval")
        return self._execute(state, action, result.token)

    def revise(self, branch_id, actions):
        """Replace the rest of a branch after a revision request; the revised action is new
        (new fingerprint) and goes through the gate again."""
        state = self.branch(branch_id)
        if state.status is not BranchStatus.NEEDS_REVISION:
            raise ValueError(f"branch {branch_id} does not need revision")
        actions = tuple(actions)
        if not actions or not all(isinstance(a, ProposedAction) for a in actions):
            raise ValueError("revision needs at least one ProposedAction")
        return self._update(state, "branch_revised", status=BranchStatus.RUNNING,
                            actions=state.actions[:state.cursor] + actions, request_id=None,
                            retry_count=0, notes="")
