"""Phase 1 exit tests plus ledger/outbox behaviour."""
import pytest

from finquint.autonomous import AutonomousWorkflow, RetryableError, RunStore
from finquint.autonomous.adapters import default_registry
from finquint.orchestration import (
    CapabilityAuthority, CapabilityError, CheckResult, EffectStatus, GateDecision, GatePolicy, GuardedTools, OutboxWorker,
    ProposedAction, SideEffect, SQLiteLedger, Tool, ToolRegistry, decide, fingerprint, idempotency_key,
)
from finquint.orchestration.adapters import action_for_call, ledger_for, status_for_task
from finquint.research import ResearchStore

SECRET = b"s" * 32


class SimulatedCrash(BaseException):
    """Process death: not an Exception, so nothing in the worker can catch it."""


class FakeRepo:
    def __init__(self):
        self.merges = []
        self.crash_after_merge = self.crash_before_merge = False

    def merge(self, action, key):
        if self.crash_before_merge:
            raise SimulatedCrash()
        self.merges.append(action.params["branch"])
        if self.crash_after_merge:
            raise SimulatedCrash()
        return {"merged": action.params["branch"], "commit": f"c{len(self.merges)}"}

    def reconcile(self, action, key):
        branch = action.params["branch"]
        return {"merged": branch, "commit": "found"} if branch in self.merges else None


def registry(repo, *, reconcile=True, writes=None):
    writes = [] if writes is None else writes
    return (ToolRegistry()
            .register(Tool("merge_to_main", SideEffect.EXTERNAL, repo.merge,
                           repo.reconcile if reconcile else None))
            .register(Tool("write_artifact", SideEffect.IDEMPOTENT_WRITE,
                           lambda a, k: writes.append(a.params) or {"written": len(writes)}))
            .register(Tool("research", SideEffect.READ_ONLY, lambda a, k: {"rows": 3})))


MERGE = ProposedAction("merge_to_main", "repo:main", {"branch": "feature/x"}, code_sha="abc")


@pytest.fixture
def db(tmp_path):
    return tmp_path / "research.sqlite"


def setup(path, repo, **options):
    research = ResearchStore(path)
    tools = registry(repo, **options)
    ledger = ledger_for(research)
    authority = CapabilityAuthority(SECRET, ttl_seconds=600)
    return research, ledger, GuardedTools(tools, ledger, authority), OutboxWorker(tools, ledger), authority


def token(authority, action):
    return authority.mint(action.action_type, fingerprint(action))


# ---- Exit test 1: a crash mid-merge causes zero duplicate merges -------------------------

def test_exit_crash_after_merge_never_merges_twice(db):
    repo = FakeRepo()
    research, ledger, guarded, worker, authority = setup(db, repo)
    outcome = guarded.call("run1", MERGE, token=token(authority, MERGE))
    assert outcome.status is EffectStatus.QUEUED
    repo.crash_after_merge = True
    with pytest.raises(SimulatedCrash):
        worker.drain()
    research.close()  # the process dies with the entry still dispatching

    repo.crash_after_merge = False
    research, ledger, guarded, worker, authority = setup(db, repo)
    # Graph resume replays the node: same key, no new outbox row.
    replay = guarded.call("run1", MERGE, token=token(authority, MERGE))
    assert replay.replayed and replay.status is EffectStatus.DISPATCHING
    settled, = worker.recover()
    assert settled.status is EffectStatus.DONE and settled.result["merged"] == "feature/x"
    assert worker.drain() == ()
    assert guarded.call("run1", MERGE, token=token(authority, MERGE)).status is EffectStatus.DONE
    assert repo.merges == ["feature/x"]
    research.close()


def test_exit_crash_before_merge_is_resent_exactly_once(db):
    repo = FakeRepo()
    research, ledger, guarded, worker, authority = setup(db, repo)
    guarded.call("run1", MERGE, token=token(authority, MERGE))
    repo.crash_before_merge = True
    with pytest.raises(SimulatedCrash):
        worker.drain()
    research.close()

    repo.crash_before_merge = False
    research, ledger, guarded, worker, authority = setup(db, repo)
    settled, = worker.recover()
    assert settled.status is EffectStatus.QUEUED
    done, = worker.drain()
    assert done.status is EffectStatus.DONE and done.attempts == 2
    assert repo.merges == ["feature/x"]
    research.close()


def test_exit_crash_without_reconcile_is_left_in_doubt_not_resent(db):
    repo = FakeRepo()
    research, ledger, guarded, worker, authority = setup(db, repo, reconcile=False)
    guarded.call("run1", MERGE, token=token(authority, MERGE))
    repo.crash_after_merge = True
    with pytest.raises(SimulatedCrash):
        worker.drain()
    research.close()

    repo.crash_after_merge = False
    research, ledger, guarded, worker, authority = setup(db, repo, reconcile=False)
    settled, = worker.recover()
    assert settled.status is EffectStatus.IN_DOUBT
    assert worker.drain() == ()
    assert repo.merges == ["feature/x"]
    events = [e["event_type"] for e in research.audit_log(subject_id="run1")]
    assert events[-1] == "side_effect_in_doubt"
    research.close()


# ---- Exit test 2: a privileged tool called without a token is refused --------------------

def test_exit_privileged_tool_without_token_is_refused(db):
    repo, writes = FakeRepo(), []
    research = ResearchStore(db)
    tools, ledger = registry(repo, writes=writes), ledger_for(research)
    guarded = GuardedTools(tools, ledger, CapabilityAuthority(SECRET, ttl_seconds=600))
    write = ProposedAction("write_artifact", "artifact:a1", {"x": 1})
    for privileged in (MERGE, write):
        with pytest.raises(CapabilityError):
            guarded.call("run1", privileged)
    assert ledger.entries() == () and repo.merges == [] and writes == []
    refused = [e for e in research.audit_log(subject_id="run1") if e["event_type"] == "tool_call_refused"]
    assert [e["payload"]["tool"] for e in refused] == ["merge_to_main", "write_artifact"]
    research.close()


def test_token_for_one_action_does_not_authorize_another(db):
    repo = FakeRepo()
    research, ledger, guarded, worker, authority = setup(db, repo)
    other = ProposedAction("merge_to_main", "repo:main", {"branch": "feature/evil"}, code_sha="abc")
    with pytest.raises(CapabilityError, match="different action"):
        guarded.call("run1", other, token=token(authority, MERGE))
    foreign = CapabilityAuthority(b"f" * 32, ttl_seconds=600)
    with pytest.raises(CapabilityError, match="signature"):
        guarded.call("run1", MERGE, token=token(foreign, MERGE))
    assert ledger.entries() == ()
    research.close()


# ---- Ledger and outbox behaviour ---------------------------------------------------------

def test_read_only_tools_need_no_token_and_leave_no_ledger_entry(db):
    research, ledger, guarded, worker, authority = setup(db, FakeRepo())
    outcome = guarded.call("run1", ProposedAction("research", "source:docs"))
    assert outcome.status is EffectStatus.DONE and outcome.result == {"rows": 3}
    assert ledger.entries() == ()
    research.close()


def test_idempotent_write_replays_stored_result(db):
    writes = []
    research = ResearchStore(db)
    ledger = ledger_for(research)
    authority = CapabilityAuthority(SECRET, ttl_seconds=600)
    guarded = GuardedTools(registry(FakeRepo(), writes=writes), ledger, authority)
    write = ProposedAction("write_artifact", "artifact:a1", {"x": 1})
    first = guarded.call("run1", write, token=token(authority, write))
    second = guarded.call("run1", write, token=token(authority, write))
    assert first.status is EffectStatus.DONE and not first.replayed
    assert second.replayed and second.result == first.result and writes == [{"x": 1}]
    third = guarded.call("run1", write, token=token(authority, write), attempt_scope="1")
    assert not third.replayed and len(writes) == 2
    research.close()


def test_interrupted_idempotent_write_is_retaken(db):
    research = ResearchStore(db)
    ledger = ledger_for(research)
    authority = CapabilityAuthority(SECRET, ttl_seconds=600)
    write = ProposedAction("write_artifact", "artifact:a1", {"x": 1})
    key = idempotency_key("run1", fingerprint(write))
    ledger.begin(key, run_id="run1", fingerprint=fingerprint(write), tool="write_artifact",
                 action=write.to_dict())  # crash before finish
    writes = []
    outcome = GuardedTools(registry(FakeRepo(), writes=writes), ledger, authority).call(
        "run1", write, token=token(authority, write))
    assert outcome.status is EffectStatus.DONE and ledger.get(key).attempts == 2 and len(writes) == 1
    research.close()


def test_failed_write_is_recorded_and_not_rerun_in_the_same_scope(db):
    research = ResearchStore(db)
    ledger = ledger_for(research)
    authority = CapabilityAuthority(SECRET, ttl_seconds=600)
    calls = []

    def flaky(action, key):
        calls.append(key)
        raise OSError("disk full")

    tools = ToolRegistry().register(Tool("write_artifact", SideEffect.IDEMPOTENT_WRITE, flaky))
    guarded = GuardedTools(tools, ledger, authority)
    write = ProposedAction("write_artifact", "artifact:a1")
    first = guarded.call("run1", write, token=token(authority, write))
    again = guarded.call("run1", write, token=token(authority, write))
    assert first.status is EffectStatus.FAILED and "disk full" in first.error
    assert again.replayed and again.status is EffectStatus.FAILED and len(calls) == 1
    research.close()


def test_handler_error_settles_by_reconcile_or_stays_in_doubt(db):
    research = ResearchStore(db)
    ledger = ledger_for(research)
    authority = CapabilityAuthority(SECRET, ttl_seconds=600)
    landed = []

    def merge_then_timeout(action, key):
        if action.params["branch"] == "landed":
            landed.append(key)
        raise TimeoutError("API timed out")

    def reconcile(action, key):
        return {"merged": True} if key in landed else None

    tools = (ToolRegistry()
             .register(Tool("merge_to_main", SideEffect.EXTERNAL, merge_then_timeout, reconcile))
             .register(Tool("deploy", SideEffect.EXTERNAL, merge_then_timeout)))
    guarded, worker = GuardedTools(tools, ledger, authority), OutboxWorker(tools, ledger)
    for action in (ProposedAction("merge_to_main", "repo:main", {"branch": "landed"}),
                   ProposedAction("merge_to_main", "repo:main", {"branch": "lost"}),
                   ProposedAction("deploy", "env:prod", {"branch": "lost"})):
        guarded.call("run1", action, token=token(authority, action))
    statuses = [e.status for e in worker.drain()]
    assert statuses == [EffectStatus.DONE, EffectStatus.FAILED, EffectStatus.IN_DOUBT]
    research.close()


def test_outbox_dispatches_in_order_and_each_entry_once(db):
    repo = FakeRepo()
    research, ledger, guarded, worker, authority = setup(db, repo)
    branches = ["a", "b", "c"]
    for branch in branches + ["a"]:
        action = ProposedAction("merge_to_main", "repo:main", {"branch": branch})
        guarded.call("run1", action, token=token(authority, action))
    assert len(worker.drain()) == 3 and repo.merges == branches
    assert worker.drain() == ()
    research.close()


def test_ledger_guards_its_state_transitions(db):
    research = ResearchStore(db)
    ledger = SQLiteLedger(research.connection)
    record = dict(run_id="r", fingerprint="f" * 64, tool="merge_to_main", action=MERGE.to_dict())
    entry, created = ledger.enqueue("k", **record)
    assert created and ledger.enqueue("k", **record) == (entry, False)
    with pytest.raises(ValueError, match="different action"):
        ledger.enqueue("k", **dict(record, tool="deploy"))
    with pytest.raises(ValueError):
        ledger.finish("k", EffectStatus.DONE, result={"ok": True})  # never dispatched
    with pytest.raises(ValueError):
        ledger.requeue("k")
    ledger.claim_next()
    with pytest.raises(ValueError):
        ledger.finish("k", EffectStatus.DONE)  # done needs a result
    with pytest.raises(ValueError):
        ledger.finish("k", EffectStatus.QUEUED)
    assert ledger.finish("k", EffectStatus.DONE, result={"ok": True}).status is EffectStatus.DONE
    with pytest.raises(ValueError):
        ledger.finish("k", EffectStatus.FAILED)
    research.close()


def test_tampered_outbox_action_is_never_sent(db):
    repo = FakeRepo()
    research, ledger, guarded, worker, authority = setup(db, repo)
    outcome = guarded.call("run1", MERGE, token=token(authority, MERGE))
    with research.connection:
        research.connection.execute("UPDATE side_effects SET action=replace(action,'feature/x','feature/evil')")
    failed, = worker.drain()
    assert failed.status is EffectStatus.FAILED and repo.merges == [] and failed.key == outcome.key
    research.close()


def test_idempotency_key_and_tool_declarations():
    assert idempotency_key("r", "f") == idempotency_key("r", "f", "0") != idempotency_key("r", "f", "1")
    assert idempotency_key("ab", "c") != idempotency_key("a", "bc")
    with pytest.raises(ValueError):
        idempotency_key("", "f")
    with pytest.raises(ValueError):
        Tool("w", SideEffect.IDEMPOTENT_WRITE, lambda a, k: {}, reconcile=lambda a, k: None)
    with pytest.raises(ValueError):
        Tool("bad name", SideEffect.READ_ONLY, lambda a, k: {})
    with pytest.raises(ValueError):
        ToolRegistry().get("missing")


# ---- Adapters into finquint.autonomous ---------------------------------------------------

def test_adapters_translate_autonomous_state(db):
    from finquint.autonomous import Call, Request, Source
    research = ResearchStore(db)
    workflow = AutonomousWorkflow(RunStore(research), default_registry())
    state = workflow.submit(Request("run1", "Price a bond", "commit-1", (
        Call("research", "source_review", {"source_ids": ["conv"]}),
        Call("analytics", "bond_analytics", {
            "bond": {"face_value": 100, "coupon_rate": .05, "maturity_years": 5, "frequency": 2},
            "annual_yield": .04})), (Source("conv", "repo://docs", "commit-1", "Nominal yields."),)))
    action = action_for_call(state, state["plan"]["calls"][1])
    assert (action.action_type, action.target, action.code_sha) == (
        "bond_analytics", "run:run1/task:analytics", "commit-1")
    assert action.params == state["plan"]["calls"][1]["parameters"]

    state = workflow.run("run1")
    budget = state["plan"]["max_attempts"]
    assert budget == 2
    assert status_for_task(state["tasks"]["analytics"], max_attempts=budget).checks is CheckResult.PASSED
    pending = status_for_task({"status": "pending", "attempts": 1, "error": "RetryableError: down"},
                              max_attempts=budget)
    assert (pending.checks, pending.retries_exhausted) == (CheckResult.INFRA_ERROR, False)
    assert status_for_task({"status": "pending", "attempts": 2, "error": "RetryableError: down"},
                           max_attempts=budget).retries_exhausted
    exhausted = status_for_task({"status": "failed", "attempts": 2, "error": "RetryableError: down"},
                                max_attempts=budget)
    assert (exhausted.checks, exhausted.retries_exhausted) == (CheckResult.INFRA_ERROR, True)
    failed = status_for_task({"status": "failed", "attempts": 2, "error": "x"}, max_attempts=budget)
    assert (failed.checks, failed.retry_count) == (CheckResult.FAILED, 1)
    with pytest.raises(ValueError):
        status_for_task({"status": "running", "attempts": 1, "error": None}, max_attempts=budget)
    with pytest.raises(ValueError):
        status_for_task({"status": "failed", "attempts": 1, "error": "x"}, max_attempts=0)

    # The ledger shares the research audit log alongside the autonomous run's events.
    ledger = ledger_for(research)
    ledger.enqueue("k", run_id="run1", fingerprint=fingerprint(MERGE), tool="merge_to_main", action=MERGE.to_dict())
    events = [e["event_type"] for e in research.audit_log(subject_id="run1")]
    assert events[0] == "autonomous_planned" and events[-1] == "side_effect_enqueued"
    research.close()


@pytest.mark.parametrize("error, expected", [
    (RetryableError("data API offline"), GateDecision.ESCALATE),   # retries ran out on infra
    (ValueError("bad output"), GateDecision.BLOCK),                 # ordinary failure
])
def test_failed_autonomous_task_routes_through_the_gate(db, error, expected):
    from dataclasses import replace
    from finquint.autonomous import Call, Request, Source

    def execute(parameters, context):
        raise error

    research = ResearchStore(db)
    methods = default_registry()
    methods._methods["bond_analytics"] = replace(methods.get("bond_analytics"), execute=execute)
    workflow = AutonomousWorkflow(RunStore(research), methods)
    max_attempts = 2
    workflow.submit(Request("run1", "Price a bond", "commit-1", (
        Call("research", "source_review", {"source_ids": ["conv"]}),
        Call("analytics", "bond_analytics", {
            "bond": {"face_value": 100, "coupon_rate": .05, "maturity_years": 5, "frequency": 2},
            "annual_yield": .04})), (Source("conv", "repo://docs", "commit-1", "Nominal yields."),),
        max_attempts))
    state = workflow.run("run1")
    task = state["tasks"]["analytics"]
    assert task["status"] == "failed"

    # The gate allows more retries (3) than the plan had left (1); the plan's budget still wins.
    policy = GatePolicy(allowed_actions={"bond_analytics"}, default_max_retries=3)
    action = action_for_call(state, state["plan"]["calls"][1])
    status = status_for_task(task, max_attempts=state["plan"]["max_attempts"])
    assert decide(action, status, policy) is expected
    research.close()
