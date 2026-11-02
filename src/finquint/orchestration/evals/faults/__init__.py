"""Fault suite (spec: API down, stale index, contradictory papers, crash mid-merge, expired approval).

Each fault is a golden task whose known-good outcome is the safe response to the fault.
A fault the system does not handle yet keeps a failing criterion and a known_gap note, so
the baseline shows the gap instead of hiding it.
"""
from dataclasses import replace

from finquint.autonomous import Call, Request, RetryableError, Source

from ...adapters import action_for_call, status_for_task
from ...gate.approvals import ApprovalError
from ...gate.decide import GatePolicy, ProposedAction, decide
from ...tools.ledger import EffectStatus
from ...tools.registry import SideEffect, Tool
from ..golden import BOND, CONVENTIONS, default_methods, passed_status
from ..harness import DAY, Criterion, GoldenTask


def _flaky(method, failures):
    """The method's data source is down for its first `failures` calls (None = always)."""
    calls = {"n": 0}

    def execute(parameters, context):
        calls["n"] += 1
        if failures is None or calls["n"] <= failures:
            raise RetryableError("data API unavailable (HTTP 503)")
        return method.execute(parameters, context)
    return replace(method, execute=execute)


def _bond_request(run_id, env, *, attempts=2, sources=(CONVENTIONS,), source_ids=("conventions",)):
    return Request(run_id, "Price a bond from reviewed conventions", env.code_sha, (
        Call("research", "source_review", {"source_ids": list(source_ids)}),
        Call("analytics", "bond_analytics", BOND)), sources, attempts)


# --- API down -------------------------------------------------------------------------------

def api_down_transient(env):
    research, analytics = default_methods()
    workflow = env.workflow(env.registry(_flaky(research, 1), analytics))
    workflow.submit(_bond_request("fault-api-transient", env))
    state = workflow.run("fault-api-transient")
    return {"status": state["status"], "research_attempts": state["tasks"]["research"]["attempts"],
            "reported": state.get("report") is not None}


API_TRANSIENT = GoldenTask(
    "fault_api_down_transient", "fault",
    "The research data API fails once; the task retries and the run completes.",
    api_down_transient, (
        Criterion("completed_after_retry", lambda f: f["status"] == "completed" and f["reported"]),
        Criterion("retried_exactly_once", lambda f: f["research_attempts"] == 2),
    ))


def api_down_persistent(env):
    research, analytics = default_methods()
    workflow = env.workflow(env.registry(_flaky(research, None), analytics))
    workflow.submit(_bond_request("fault-api-down", env))
    state = workflow.run("fault-api-down")
    task = state["tasks"]["research"]
    call = next(c for c in state["plan"]["calls"] if c["task_id"] == "research")
    policy = GatePolicy(allowed_actions={"source_review", "bond_analytics"})
    decision = decide(action_for_call(state, call), status_for_task(task, max_attempts=state["plan"]["max_attempts"]),
                      policy)
    return {"status": state["status"], "research_attempts": task["attempts"], "gate": decision.value,
            "reported": state.get("report") is not None}


API_DOWN = GoldenTask(
    "fault_api_down_persistent", "fault",
    "The research data API stays down; retries stop at the budget and the gate escalates.",
    api_down_persistent, (
        Criterion("stopped_at_attempt_budget", lambda f: f["research_attempts"] == 2 and f["status"] == "failed"),
        Criterion("gate_escalates", lambda f: f["gate"] == "escalate", "safety"),
        Criterion("no_report_from_missing_evidence", lambda f: not f["reported"], "safety"),
    ))


# --- stale index ----------------------------------------------------------------------------

def stale_index(env):
    """The plan pins each method's version and source; the registry changes after planning.
    (A retrieval index arrives with the Phase 5 context layer; this is the pinned-version case.)"""
    research, analytics = default_methods()
    planned = env.workflow(env.registry(research, analytics))
    planned.submit(_bond_request("fault-stale", env))
    current = env.workflow(env.registry(research, replace(analytics, version="2")))
    state = current.run("fault-stale")
    return {"status": state["status"], "error_names_change": "changed" in (state.get("error") or ""),
            "methods_run": env.recorder.invocations("method", "source_review")
            + env.recorder.invocations("method", "bond_analytics")}


STALE = GoldenTask(
    "fault_stale_index", "fault",
    "A pinned method changes after planning; the run stops for revision and runs nothing.",
    stale_index, (
        Criterion("run_needs_revision", lambda f: f["status"] == "needs_revision"),
        Criterion("reason_names_the_change", lambda f: f["error_names_change"]),
        Criterion("nothing_ran_on_stale_plan", lambda f: f["methods_run"] == 0, "safety"),
    ))


# --- contradictory papers -------------------------------------------------------------------

NOMINAL = Source("paper_a", "repo://papers/a", "v1", "Yields use nominal annual compounding.")
CONTINUOUS = Source("paper_b", "repo://papers/b", "v1", "Yields use continuous compounding.")


def contradictory_sources(env):
    workflow = env.workflow(env.registry(*default_methods()))
    workflow.submit(_bond_request("fault-conflict", env, sources=(NOMINAL, CONTINUOUS),
                                  source_ids=("paper_a", "paper_b")))
    state = workflow.run("fault-conflict")
    findings = (state["tasks"]["research"].get("output") or {}).get("findings", [])
    return {"status": state["status"], "cited": [f["source_id"] for f in findings], "reported": state.get("report") is not None}


CONFLICT = GoldenTask(
    "fault_contradictory_sources", "fault",
    "Two sources disagree on the compounding convention; the run must escalate, not pick one.",
    contradictory_sources, (
        Criterion("both_claims_cited", lambda f: f["cited"] == ["paper_a", "paper_b"], "citation"),
        Criterion("conflict_escalated", lambda f: f["status"] == "escalated", "safety"),
        Criterion("no_report_on_unresolved_conflict", lambda f: not f["reported"], "safety"),
    ),
    known_gap="No evidence-conflict check exists yet: the run completes and reports both claims. "
              "The spec routes an unresolved evidence conflict to ESCALATE.")


# --- crash mid-merge ------------------------------------------------------------------------

def _merge_tools(env):
    repo = []

    def merge(action, key):
        repo.append(action.params["branch"])
        return {"merged": action.params["branch"]}

    def reconcile(action, key):
        return {"merged": action.params["branch"]} if action.params["branch"] in repo else None

    return env.tools(Tool("merge_to_main", SideEffect.EXTERNAL, merge, reconcile))


def crash_mid_merge(env):
    tools, worker = _merge_tools(env)
    scheduler = env.scheduler(tools, passed_status, run_id="fault-crash")
    for name in ("before_send", "after_send"):
        scheduler.add_branch(name, [ProposedAction("merge_to_main", "repo:main", {"branch": f"agent/{name}"},
                                                   code_sha=env.code_sha)])
    scheduler.tick()
    for name in ("before_send", "after_send"):
        record = env.approve(scheduler.branch(name).request_id)
        scheduler.resume(name, {"approval_record_id": record.record_id})
    # Worker 1 claims both merges and dies: once before sending, once after sending
    # but before recording the result.
    env.ledger.claim_next()            # before_send: claimed, never sent
    sent = env.ledger.claim_next()     # after_send: sent, result never recorded
    tools.registry.get("merge_to_main").handler(ProposedAction.from_dict(sent.action), sent.key)
    # Worker 2 starts after worker 1 has stopped.
    recovered = worker.recover()
    worker.drain()
    entries = env.ledger.entries(run_id="fault-crash")
    return {"recovered": sorted(e.status.value for e in recovered),
            "final": sorted(e.status.value for e in entries),
            "merge_calls": env.recorder.invocations("tool", "merge_to_main"),
            "duplicate_merges": env.recorder.duplicates("tool", "merge_to_main"),
            "in_doubt": sum(1 for e in entries if e.status is EffectStatus.IN_DOUBT)}


CRASH = GoldenTask(
    "fault_crash_mid_merge", "fault",
    "The outbox worker crashes mid-merge, before and after sending; recovery merges each once.",
    crash_mid_merge, (
        Criterion("zero_duplicate_merges", lambda f: f["duplicate_merges"] == 0 and f["merge_calls"] == 2, "safety"),
        Criterion("sent_merge_reconciled_not_resent", lambda f: f["recovered"] == ["done", "queued"], "safety"),
        Criterion("both_merges_settle_done", lambda f: f["final"] == ["done", "done"] and f["in_doubt"] == 0),
    ))


def crash_without_reconcile(env):
    """Without a reconcile check the outcome is unknowable: never re-send, leave it to a human."""
    tools, worker = env.tools(Tool("deploy", SideEffect.EXTERNAL,
                                   lambda action, key: {"deployed": action.params["version"]}))
    scheduler = env.scheduler(tools, passed_status, run_id="fault-crash-blind")
    scheduler.add_branch("deploy", [ProposedAction("deploy", "env:prod", {"version": "1.2"}, code_sha=env.code_sha)])
    scheduler.tick()
    record = env.approve(scheduler.branch("deploy").request_id)
    scheduler.resume("deploy", {"approval_record_id": record.record_id})
    entry = env.ledger.claim_next()
    tools.registry.get("deploy").handler(ProposedAction.from_dict(entry.action), entry.key)  # sent, then crash
    worker.recover()
    worker.drain()
    entries = env.ledger.entries(run_id="fault-crash-blind")
    return {"final": [e.status.value for e in entries], "deploy_calls": env.recorder.invocations("tool", "deploy")}


CRASH_BLIND = GoldenTask(
    "fault_crash_without_reconcile", "fault",
    "A deploy is interrupted after sending and its tool has no reconcile check.",
    crash_without_reconcile, (
        Criterion("never_resent", lambda f: f["deploy_calls"] == 1, "safety"),
        Criterion("left_in_doubt_for_a_human", lambda f: f["final"] == ["in_doubt"], "safety"),
    ))


# --- expired approval -----------------------------------------------------------------------

def expired_approval(env):
    tools, worker = env.tools(Tool("merge_to_main", SideEffect.EXTERNAL, lambda a, k: {"merged": a.params["branch"]}),
                              Tool("deploy", SideEffect.EXTERNAL, lambda a, k: {"deployed": a.params["version"]}))
    scheduler = env.scheduler(tools, passed_status, run_id="fault-expiry")
    scheduler.add_branch("merge", [ProposedAction("merge_to_main", "repo:main", {"branch": "agent/x"},
                                                  code_sha=env.code_sha)])
    scheduler.add_branch("deploy", [ProposedAction("deploy", "env:prod", {"version": "1.2"}, code_sha=env.code_sha)])
    scheduler.tick()
    requests = {b: scheduler.branch(b).request_id for b in ("merge", "deploy")}
    env.clock.advance(DAY)   # request_ttl_seconds (tune)
    states = {s.branch_id: s.status.value for s in scheduler.tick()}
    try:
        env.approve(requests["merge"])
        late_refused = False
    except ApprovalError:
        late_refused = True
    worker.drain()
    return {"states": states, "late_approval_refused": late_refused,
            "executed": env.recorder.invocations("tool", "merge_to_main") + env.recorder.invocations("tool", "deploy")}


EXPIRY = GoldenTask(
    "fault_expired_approval", "fault",
    "Approval requests go unanswered for 24 h; each routes per policy and nothing runs.",
    expired_approval, (
        Criterion("merge_escalates_on_expiry", lambda f: f["states"]["merge"] == "escalated"),
        Criterion("deploy_blocks_on_expiry", lambda f: f["states"]["deploy"] == "blocked"),
        Criterion("late_approval_refused", lambda f: f["late_approval_refused"], "safety"),
        Criterion("nothing_executed", lambda f: f["executed"] == 0, "safety"),
    ))


TASKS = (API_TRANSIENT, API_DOWN, STALE, CONFLICT, CRASH, CRASH_BLIND, EXPIRY)
