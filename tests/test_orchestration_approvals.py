"""Phase 3 exit tests (approval path) plus approval-record and scheduler behaviour."""
import json
import sqlite3

import pytest

from finquint.autonomous import AutonomousWorkflow, Call, Method, RunStore
from finquint.autonomous.adapters import default_registry
from finquint.orchestration import (
    ActionStatus, ApprovalError, ApprovalOutcome, ApprovalPolicy, ApprovalService, BranchStatus,
    CapabilityAuthority, CheckResult, GuardedTools, HumanGate, OutboxWorker, ProposedAction, RequestStatus,
    Requirement, Route, Scheduler, SideEffect, Tool, ToolRegistry, default_policy,
)
from finquint.orchestration import approve as approve_cli
from finquint.orchestration.adapters import ledger_for
from finquint.research import ResearchStore

from test_autonomous_workflow import ok, request as autonomous_request

APPROVAL_KEY = b"a" * 32
CAPABILITY_KEY = b"c" * 32
DAY = 24 * 3600


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def policy(**changes):
    base = dict(reviewers={"walleva1715": {"owner"}, "carol": {"owner"}, "bob": {"analyst"}},
                requirements={"merge_to_main": Requirement("owner"), "deploy": Requirement("owner", 2),
                              "improve": Requirement("owner")},
                request_ttl_seconds=DAY, approval_ttl_seconds=3600, on_expiry={"deploy": "block"})
    base.update(changes)
    return ApprovalPolicy(**base)


class Repo:
    def __init__(self):
        self.merges, self.deploys = [], []

    def merge(self, action, key):
        self.merges.append(action.params["branch"])
        return {"merged": action.params["branch"]}

    def deploy(self, action, key):
        self.deploys.append(action.params["version"])
        return {"deployed": action.params["version"]}


class Host:
    """Everything a trusted host wires together, on one SQLite database."""

    def __init__(self, path, *, approval_policy=None, preconditions=None):
        self.research = ResearchStore(path)
        self.db = self.research.connection
        self.clock, self.repo, self.researched = Clock(), Repo(), []
        self.checks = {}  # action_type -> CheckResult
        self.authority = CapabilityAuthority(CAPABILITY_KEY, ttl_seconds=600, clock=self.clock)
        self.approvals = ApprovalService(self.db, secret=APPROVAL_KEY, policy=approval_policy or policy(),
                                         authority=self.authority, clock=self.clock, audit=self.research._event)
        self.gate = HumanGate(self.approvals)
        self.registry = (ToolRegistry()
                         .register(Tool("merge_to_main", SideEffect.EXTERNAL, self.repo.merge))
                         .register(Tool("deploy", SideEffect.EXTERNAL, self.repo.deploy))
                         .register(Tool("research", SideEffect.READ_ONLY,
                                        lambda a, k: self.researched.append(a.target) or {"ok": True})))
        self.ledger = ledger_for(self.research)
        self.tools = GuardedTools(self.registry, self.ledger, self.authority)
        self.worker = OutboxWorker(self.registry, self.ledger)
        self.preconditions = preconditions
        self.scheduler = self.new_scheduler()

    def status_for(self, branch_id, action, retry_count):
        return ActionStatus(self.checks.get(action.action_type, CheckResult.PASSED), retry_count)

    def new_scheduler(self):
        return Scheduler(self.db, run_id="run1", policy=default_policy(), tools=self.tools,
                         authority=self.authority, gate=self.gate, status_for=self.status_for,
                         preconditions=self.preconditions,
                         evidence_for=lambda a: {"tests": "293 passed"}, audit=self.research._event)

    def events(self, event_type):
        return [e for e in self.research.audit_log(subject_id="run1") if e["event_type"] == event_type]


@pytest.fixture
def host(tmp_path):
    h = Host(tmp_path / "research.sqlite")
    yield h
    h.research.close()


MERGE = ProposedAction("merge_to_main", "repo:main", {"branch": "agent/x"}, code_sha="abc")
DEPLOY = ProposedAction("deploy", "env:prod", {"version": "1.2"}, code_sha="abc")
RESEARCH = [ProposedAction("research", f"source:{i}", {"q": i}) for i in range(3)]


def waiting_merge(host, action=MERGE):
    host.scheduler.add_branch("merge", [action])
    host.scheduler.tick()
    state = host.scheduler.branch("merge")
    assert state.status is BranchStatus.WAITING and state.request_id
    return state


def resume(host, record, branch="merge"):
    return host.scheduler.resume(branch, {"approval_record_id": record.record_id})


# --- exit test 1: approve, revise, reject and expire each route correctly ---------------------

def test_exit_approve_routes_to_execution(host):
    state = waiting_merge(host)
    request = host.approvals.get_request(state.request_id)
    assert request.action == MERGE.to_dict() and request.evidence == {"tests": "293 passed"}

    record = host.approvals.decide(state.request_id, reviewer="walleva1715", decision="approve")
    assert resume(host, record).status is BranchStatus.DONE
    host.worker.drain()
    assert host.repo.merges == ["agent/x"]
    assert [e["payload"]["record_id"] for e in host.events("approval_verified")] == [record.record_id]


def test_exit_revise_routes_to_replanning(host):
    state = waiting_merge(host)
    record = host.approvals.decide(state.request_id, reviewer="walleva1715", decision="revise",
                                   notes="Squash the commits first")
    revised = resume(host, record)
    assert revised.status is BranchStatus.NEEDS_REVISION and revised.notes == "Squash the commits first"
    assert host.repo.merges == []

    new_action = ProposedAction("merge_to_main", "repo:main", {"branch": "agent/x-squashed"}, code_sha="abc")
    host.scheduler.revise("merge", [new_action])
    host.scheduler.tick()
    again = host.scheduler.branch("merge")
    assert again.status is BranchStatus.WAITING and again.request_id != state.request_id
    assert host.approvals.get_request(again.request_id).fingerprint != host.approvals.get_request(state.request_id).fingerprint


def test_exit_reject_routes_to_block(host):
    state = waiting_merge(host)
    record = host.approvals.decide(state.request_id, reviewer="walleva1715", decision="reject",
                                   notes="Out of scope")
    blocked = resume(host, record)
    assert blocked.status is BranchStatus.BLOCKED and blocked.notes == "Out of scope"
    host.worker.drain()
    assert host.repo.merges == []


def test_exit_expiry_routes_by_policy(host):
    waiting_merge(host)
    host.scheduler.add_branch("deploy", [DEPLOY])
    host.scheduler.tick()
    deploy_request = host.scheduler.branch("deploy").request_id

    host.clock.now += DAY  # spec default 24 h
    host.scheduler.tick()
    assert host.scheduler.branch("merge").status is BranchStatus.ESCALATED   # default policy
    assert host.scheduler.branch("deploy").status is BranchStatus.BLOCKED    # on_expiry: block
    assert "expired" in host.scheduler.branch("merge").notes
    with pytest.raises(ApprovalError, match="expired"):
        host.approvals.decide(deploy_request, reviewer="walleva1715", decision="approve")
    assert host.repo.merges == [] and host.repo.deploys == []


# --- exit test 2: research keeps running during a pending merge approval ---------------------

def test_exit_research_branch_keeps_running_while_merge_waits(host):
    host.scheduler.add_branch("research", RESEARCH)
    host.scheduler.add_branch("merge", [MERGE])
    host.scheduler.tick()
    assert host.scheduler.branch("merge").status is BranchStatus.WAITING
    states = {s.branch_id: s for s in host.scheduler.run_until_idle()}
    assert states["research"].status is BranchStatus.DONE
    assert host.researched == ["source:0", "source:1", "source:2"]
    assert states["merge"].status is BranchStatus.WAITING  # still paused, alone

    record = host.approvals.decide(states["merge"].request_id, reviewer="walleva1715", decision="approve")
    assert resume(host, record).status is BranchStatus.DONE


# --- the record is the only way through ------------------------------------------------------

def test_resume_takes_only_an_approval_record_id(host):
    waiting_merge(host)
    for payload in ({"approved": True}, {"approval_record_id": "x", "approved": True}, "approve", {}):
        with pytest.raises(ValueError):
            host.scheduler.resume("merge", payload)
    with pytest.raises(ValueError, match="does not belong"):
        host.scheduler.resume("merge", {"approval_record_id": "no-such-record"})


def test_record_for_another_request_cannot_resume_a_branch(host):
    waiting_merge(host)
    host.scheduler.add_branch("deploy", [DEPLOY])
    host.scheduler.tick()
    other = host.approvals.decide(host.scheduler.branch("deploy").request_id, reviewer="carol", decision="approve")
    with pytest.raises(ValueError, match="does not belong"):
        resume(host, other)


def test_approval_cannot_be_used_for_a_different_action(host):
    state = waiting_merge(host)
    record = host.approvals.decide(state.request_id, reviewer="walleva1715", decision="approve")
    other = ProposedAction("merge_to_main", "repo:main", {"branch": "agent/evil"}, code_sha="abc")
    result = host.gate.resume(other, {"approval_record_id": record.record_id})
    assert result.route is Route.BLOCK and result.token is None
    assert "different action" in result.reason


def test_approval_is_single_use(host):
    state = waiting_merge(host)
    record = host.approvals.decide(state.request_id, reviewer="walleva1715", decision="approve")
    first = host.approvals.verify(record.record_id, MERGE)
    second = host.approvals.verify(record.record_id, MERGE)
    assert first.outcome is ApprovalOutcome.APPROVED and first.token is not None
    assert second.outcome is ApprovalOutcome.INVALID and "already been used" in second.reason


def test_records_are_immutable_and_tampering_is_detected(host):
    state = waiting_merge(host)
    record = host.approvals.decide(state.request_id, reviewer="walleva1715", decision="reject", notes="no")
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        host.db.execute("UPDATE approval_records SET decision='approve' WHERE record_id=?", (record.record_id,))
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        host.db.execute("DELETE FROM approval_records")
    # Someone with raw database access drops the guard and flips the decision.
    host.db.rollback()
    host.db.execute("DROP TRIGGER approval_records_no_update")
    host.db.execute("UPDATE approval_records SET decision='approve' WHERE record_id=?", (record.record_id,))
    host.db.execute("UPDATE approval_requests SET status='approved'")
    host.db.commit()
    assert resume(host, record).status is BranchStatus.BLOCKED
    assert host.events("approval_invalid")


def test_records_signed_with_another_key_are_invalid(host):
    state = waiting_merge(host)
    forger = ApprovalService(host.db, secret=b"f" * 32, policy=policy(), authority=host.authority, clock=host.clock)
    forged = forger.decide(state.request_id, reviewer="walleva1715", decision="approve")
    assert host.approvals.verify(forged.record_id, MERGE).outcome is ApprovalOutcome.INVALID


# --- reviewer authority, counts and expiry ---------------------------------------------------

def test_only_authorized_reviewers_can_decide_and_refusals_are_audited(host):
    state = waiting_merge(host)
    with pytest.raises(ApprovalError, match="may not decide"):
        host.approvals.decide(state.request_id, reviewer="bob", decision="approve")
    with pytest.raises(ApprovalError, match="may not decide"):
        host.approvals.decide(state.request_id, reviewer="mallory", decision="approve")
    assert len(host.events("approval_refused")) == 2  # committed despite the refusal
    assert host.approvals.get_request(state.request_id).status is RequestStatus.PENDING


def test_reviewer_removed_from_policy_invalidates_their_approval(host):
    state = waiting_merge(host)
    record = host.approvals.decide(state.request_id, reviewer="walleva1715", decision="approve")
    later = ApprovalService(host.db, secret=APPROVAL_KEY, authority=host.authority, clock=host.clock,
                            policy=policy(reviewers={"carol": {"owner"}}))
    assert later.verify(record.record_id, MERGE).outcome is ApprovalOutcome.INVALID


def test_two_approver_action_waits_for_both(host):
    host.scheduler.add_branch("deploy", [DEPLOY])
    host.scheduler.tick()
    request_id = host.scheduler.branch("deploy").request_id
    first = host.approvals.decide(request_id, reviewer="walleva1715", decision="approve")
    assert resume(host, first, "deploy").status is BranchStatus.WAITING
    with pytest.raises(ApprovalError, match="already decided"):
        host.approvals.decide(request_id, reviewer="walleva1715", decision="approve")
    second = host.approvals.decide(request_id, reviewer="carol", decision="approve")
    assert resume(host, second, "deploy").status is BranchStatus.DONE
    host.worker.drain()
    assert host.repo.deploys == ["1.2"]


def test_approval_expires_if_not_used_in_time(host):
    state = waiting_merge(host)
    record = host.approvals.decide(state.request_id, reviewer="walleva1715", decision="approve")
    host.clock.now += 3600  # approval_ttl_seconds
    expired = resume(host, record)
    assert expired.status is BranchStatus.ESCALATED and host.repo.merges == []


def test_revise_and_reject_need_notes(host):
    state = waiting_merge(host)
    for decision in ("revise", "reject"):
        with pytest.raises(ApprovalError, match="notes"):
            host.approvals.decide(state.request_id, reviewer="walleva1715", decision=decision)


def test_requests_are_idempotent_and_need_a_rule(host):
    first = host.gate.open("run1", "merge", MERGE, reason="r")
    assert host.gate.open("run1", "merge", MERGE, reason="r").request_id == first.request_id
    with pytest.raises(ValueError, match="no approval requirement"):
        host.gate.open("run1", "b", ProposedAction("research", "x", {}), reason="r")


# --- re-check before execution (spec step 7) and restart -------------------------------------

def test_failed_preconditions_after_approval_escalate(tmp_path):
    moved = {"value": False}
    h = Host(tmp_path / "r.sqlite", preconditions=lambda a: ["main moved since approval"] if moved["value"] else [])
    try:
        state = waiting_merge(h)
        record = h.approvals.decide(state.request_id, reviewer="walleva1715", decision="approve")
        moved["value"] = True
        result = resume(h, record)
        assert result.status is BranchStatus.ESCALATED and "main moved" in result.notes
        h.worker.drain()
        assert h.repo.merges == []
    finally:
        h.research.close()


def test_checks_turning_red_after_approval_escalate(host):
    state = waiting_merge(host)
    record = host.approvals.decide(state.request_id, reviewer="walleva1715", decision="approve")
    host.checks["merge_to_main"] = CheckResult.FAILED
    assert resume(host, record).status is BranchStatus.ESCALATED
    assert host.repo.merges == []


def test_waiting_branch_survives_a_restart(host):
    state = waiting_merge(host)
    record = host.approvals.decide(state.request_id, reviewer="walleva1715", decision="approve")
    host.scheduler = host.new_scheduler()  # new process, same database
    assert host.scheduler.branch("merge").status is BranchStatus.WAITING
    assert resume(host, record).status is BranchStatus.DONE


def test_blocked_and_auto_actions_through_scheduler(host):
    host.scheduler.add_branch("bad", [ProposedAction("open_pr", "path:tests/test_x.py", {})])
    host.scheduler.add_branch("unknown", [ProposedAction("shell", "x", {})])
    states = {s.branch_id: s.status for s in host.scheduler.run_until_idle()}
    assert states == {"bad": BranchStatus.BLOCKED, "unknown": BranchStatus.BLOCKED}


def test_policy_file_loads():
    loaded = ApprovalPolicy.from_file("configs/approval_policy.json")
    assert loaded.may_approve("walleva1715", "merge_to_main")
    assert loaded.requirement("deploy").approvals == 1
    assert loaded.expiry_decision("merge_to_main").value == "escalate"
    with pytest.raises(ValueError):
        ApprovalPolicy(reviewers={}, requirements={}, on_expiry={"x": "retry"})


# --- reviewer CLI ----------------------------------------------------------------------------

def test_cli_lists_shows_and_decides(host, tmp_path, monkeypatch, capsys):
    import time

    host.clock.now = time.time()  # the CLI uses the real clock
    state = waiting_merge(host)
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps({"reviewers": {"walleva1715": ["owner"]},
                                       "requirements": {"merge_to_main": {"role": "owner"}}}))
    args = ["--db", str(host.research.path), "--policy", str(policy_file)]
    monkeypatch.setenv(approve_cli.KEY_VARIABLE, "")
    with pytest.raises(SystemExit):
        approve_cli.main(args + ["list"])
    monkeypatch.setenv(approve_cli.KEY_VARIABLE, APPROVAL_KEY.hex())

    assert approve_cli.main(args + ["list"]) == 0
    assert state.request_id in capsys.readouterr().out
    assert approve_cli.main(args + ["show", state.request_id]) == 0
    assert json.loads(capsys.readouterr().out)["fingerprint"] == host.approvals.get_request(state.request_id).fingerprint
    assert approve_cli.main(args + ["decide", state.request_id, "--reviewer", "bob", "--decision", "approve"]) == 1
    capsys.readouterr()
    assert approve_cli.main(args + ["decide", state.request_id, "--reviewer", "walleva1715",
                                    "--decision", "approve"]) == 0
    record_id = json.loads(capsys.readouterr().out)["approval_record_id"]
    assert host.scheduler.resume("merge", {"approval_record_id": record_id}).status is BranchStatus.DONE


# --- AutonomousWorkflow opt-in ---------------------------------------------------------------

@pytest.fixture
def gated(host):
    store, registry = RunStore(host.research), default_registry()
    registry.register(Method("improve", "1", "proposal", lambda p, c: {"patch": "diff", "rationale": "r"},
                             lambda p: None, (ok,)))
    workflow = AutonomousWorkflow(store, registry, approvals=host.gate)
    workflow.submit(autonomous_request(extra=(Call("proposal", "improve", {}, ("analytics",)),)))
    state = workflow.run("run")
    assert state["status"] == "awaiting_approval"
    assert state["tasks"]["analytics"]["status"] == "succeeded"  # independent work finished
    return workflow, state["tasks"]["proposal"]["approval_request_id"]


def test_gated_workflow_refuses_unsigned_decisions(gated):
    workflow, _ = gated
    state = workflow.store.load("run")
    with pytest.raises(ValueError, match="signed approvals"):
        workflow.decide("run", "proposal", plan_digest=state["plan_digest"], reviewer="h", approved=True, notes="n")


def test_gated_workflow_runs_after_signed_approval(gated, host):
    workflow, request_id = gated
    assert workflow.run("run")["tasks"]["proposal"]["approval_request_id"] == request_id  # no duplicate request
    record = host.approvals.decide(request_id, reviewer="walleva1715", decision="approve")
    assert workflow.resume_with_approval("run", "proposal", {"approval_record_id": record.record_id})["status"] == "ready"
    state = workflow.run("run")
    assert state["status"] == "completed" and state["tasks"]["proposal"]["output"]["patch"] == "diff"


@pytest.mark.parametrize("decision, notes, task_status", [("revise", "narrow it", "needs_revision"),
                                                          ("reject", "no", "rejected")])
def test_gated_workflow_routes_revise_and_reject(gated, host, decision, notes, task_status):
    workflow, request_id = gated
    record = host.approvals.decide(request_id, reviewer="walleva1715", decision=decision, notes=notes)
    state = workflow.resume_with_approval("run", "proposal", {"approval_record_id": record.record_id})
    assert state["tasks"]["proposal"]["status"] == task_status and state["tasks"]["proposal"]["error"] == notes


def test_gated_workflow_escalates_expired_requests(gated, host):
    workflow, _ = gated
    host.clock.now += DAY
    state = workflow.expire_approvals("run")
    assert state["status"] == "escalated" and state["tasks"]["proposal"]["status"] == "escalated"
    assert workflow.run("run")["status"] == "escalated"  # not re-claimed
