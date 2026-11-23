"""Phase 8: failure recovery ladder (ADR-020)."""
from dataclasses import replace
import json

import pytest

from finquint.autonomous import AutonomousWorkflow, RunStore
from finquint.autonomous.adapters import default_registry, risk_registry
from finquint.orchestration.evals import recovery as suite
from finquint.orchestration.gate.approvals import ApprovalPolicy
from finquint.orchestration.models import ModelCaller, ModelReply, Usage
from finquint.orchestration.recovery import (Bound, KnownFix, RecoveryPolicy, RecoveryRule, check_proposal,
                                             failure_signature, flatten, param_diff, sampled, triage,
                                             verdict_problems)
from finquint.orchestration.recovery.agents import default_card
from finquint.orchestration.recovery.policy import CONFIG_PATH
from finquint.orchestration.recovery.simulated import SimulatedRepairer, SimulatedValidator
from finquint.orchestration.verify.paths import DEFAULT_PROTECTED_PATHS
from finquint.research import ResearchStore

POLICY = RecoveryPolicy.from_file(CONFIG_PATH)
VOL_RULE = POLICY.rule("rolling_volatility")


# --- policy -------------------------------------------------------------------------------------

def test_policy_file_matches_the_settled_decisions():
    assert POLICY.max_repair_attempts == 2
    assert POLICY.validator_sample_rate == 0.0 and POLICY.validator_model is None
    assert (POLICY.lesson_threshold, POLICY.breaker_trips, POLICY.breaker_window) == (3, 3, 10)
    assert POLICY.cost_cap_usd == 0.5
    assert set(VOL_RULE.frozen) == {"returns", "annualization"}
    assert POLICY.rule("historical_var").alternatives == ("parametric_var",)
    assert POLICY.rule("bond_analytics") is None and POLICY.known_fixes == ()


@pytest.mark.parametrize("bad", [
    dict(breaker_trips=11, breaker_window=10),
    dict(validator_sample_rate=1.5),
    dict(max_repair_attempts=0),
    dict(cost_cap_usd=-1),
])
def test_invalid_policies_are_refused(bad):
    with pytest.raises(ValueError):
        RecoveryPolicy(**bad)


def test_a_parameter_cannot_be_tunable_and_frozen():
    with pytest.raises(ValueError):
        RecoveryRule(tunable={"x": Bound(1, 2)}, frozen=("x",))


def test_bounds():
    assert Bound(5, 250, integer=True).allows(20) and not Bound(5, 250, integer=True).allows(20.5)
    assert not Bound(5, 250).allows(True) and not Bound(5, 250).allows(251)
    assert Bound(values=("a", 2)).allows(2) and not Bound(values=("a", 2)).allows("b")
    with pytest.raises(ValueError):
        Bound.from_dict({"min": 1, "max": 2, "type": "float"})
    with pytest.raises(ValueError):
        RecoveryRule()   # a rule must allow something


def test_approval_policy_has_an_escalation_rule_and_recovery_is_protected():
    approvals = ApprovalPolicy.from_file("configs/approval_policy.json")
    assert approvals.requirement("recovery_escalation").approvals == 1
    assert approvals.expiry_decision("recovery_escalation").value == "escalate"
    assert "src/finquint/orchestration/recovery" in DEFAULT_PROTECTED_PATHS
    assert "/src/finquint/orchestration/recovery/" in open(".github/CODEOWNERS", encoding="utf-8").read()


# --- methods --------------------------------------------------------------------------------------

def test_risk_methods_are_added_without_changing_default_registry():
    with pytest.raises(ValueError):
        default_registry().get("rolling_volatility")
    registry = risk_registry()
    vol = registry.get("rolling_volatility")
    params = {"returns": suite.RETURNS, "window": 20, "annualization": 252}
    output = vol.execute(params, {})
    assert output["complete_windows"] == 21 and all(g(params, {}, output).passed for g in vol.gates)
    short = dict(params, window=60)
    assert not vol.gates[0](short, {}, vol.execute(short, {})).passed
    hist, para = registry.get("historical_var"), registry.get("parametric_var")
    var_params = {"pnl": suite.PNL, "confidence": 0.99}
    assert "needs at least 100 observations; got 40" in hist.gates[0](var_params, {}, hist.execute(var_params, {})).details
    assert para.gates[0](var_params, {}, para.execute(var_params, {})).passed


# --- triage ---------------------------------------------------------------------------------------

def _state(status, tasks, method="rolling_volatility"):
    calls = [{"task_id": k, "method": method if k == "risk" else "source_review", "parameters": {"window": 60},
              "dependencies": []} for k in tasks]
    return {"status": status, "plan": {"run_id": "r", "calls": calls, "objective": "o", "code_version": "s"},
            "tasks": tasks}


def _task(status, error=None):
    return {"status": status, "error": error, "attempts": 1, "output": None, "gates": []}


GATE_ERROR = ("ValueError: hard validation gate failed: [{'name': 'rolling_volatility_window', 'passed': False, "
              "'details': 'no complete window: 40 returns, window 60', 'metrics': {}}]")


def test_triage_tiers():
    assert triage(_state("completed", {"risk": _task("succeeded")}), POLICY).tier == "completed"
    assert triage(_state("failed", {"risk": _task("failed", "RetryableError: 503")}), POLICY).tier == "transient"
    assert triage(_state("needs_revision", {"risk": _task("pending")}), POLICY).tier == "forbidden"
    both = {"src": _task("failed", "ValueError: x"), "risk": _task("failed", GATE_ERROR)}
    assert triage(_state("failed", both), POLICY).tier == "multiple"
    fixable = triage(_state("failed", {"risk": _task("failed", GATE_ERROR)}), POLICY)
    assert (fixable.tier, fixable.signature) == ("fixable", "gate:rolling_volatility_window")
    assert triage(_state("failed", {"risk": _task("failed", GATE_ERROR)}, "bond_analytics"), POLICY).tier == "unrepairable"
    known = replace(POLICY, known_fixes=(KnownFix("rolling_volatility", "gate:rolling_volatility_window",
                                                  {"parameters": {"window": 20}}),))
    assert triage(_state("failed", {"risk": _task("failed", GATE_ERROR)}), known).tier == "known_fix"


def test_failure_signatures():
    assert failure_signature(_task("failed", "ValueError: input validation failed: x")) == "input"
    assert failure_signature(_task("needs_revision", "x")) == "needs_revision"
    assert failure_signature(_task("failed", "ZeroDivisionError: boom")) == "error:ZeroDivisionError"
    assert failure_signature(_task("failed", "weird text")) == "error:unknown"


# --- guards ---------------------------------------------------------------------------------------

CALL = {"task_id": "risk", "method": "rolling_volatility",
        "parameters": {"returns": [0.01, -0.02, 0.03], "window": 60, "annualization": 252}}
PACKET = "no complete window: 40 returns, window 60"


def _check(proposal, changes, evidence=PACKET, earlier=(), rule=VOL_RULE, call=CALL):
    reflection = {"evidence": evidence, "changes": changes}
    return check_proposal(call, proposal, reflection, rule=rule, registry=risk_registry(), earlier=earlier,
                          packet_text=PACKET)


def test_guard_accepts_an_honest_in_bounds_change():
    result = _check({"method": "rolling_volatility", "parameters": dict(CALL["parameters"], window=20)}, ["window"])
    assert result.passed and result.diff == ("window",)


@pytest.mark.parametrize("proposal,changes,expected", [
    ({"method": "rolling_volatility", "parameters": dict(CALL["parameters"], window=2)}, ["window"], "outside its bounds"),
    ({"method": "rolling_volatility", "parameters": dict(CALL["parameters"], annualization=1)}, ["annualization"],
     "changes frozen parameter"),
    ({"method": "rolling_volatility", "parameters": dict(CALL["parameters"], extra=1)}, ["extra"],
     "changes non-tunable parameter"),
    ({"method": "parametric_var", "parameters": CALL["parameters"]}, ["method"], "not an allowed alternative"),
    ({"method": "nope", "parameters": CALL["parameters"]}, ["method"], "not registered"),
    ({"method": "rolling_volatility", "parameters": CALL["parameters"]}, [], "repeats the original call"),
    ({"method": "rolling_volatility", "parameters": dict(CALL["parameters"], window=20)}, ["annualization"],
     "reflection claims changes"),
])
def test_guard_rejections(proposal, changes, expected):
    result = _check(proposal, changes)
    assert not result.passed and any(expected in p for p in result.problems), result.problems


def test_guard_novelty_evidence_and_protected_paths():
    proposal = {"method": "rolling_volatility", "parameters": dict(CALL["parameters"], window=20)}
    first = _check(proposal, ["window"])
    again = _check(proposal, ["window"], earlier={first.fingerprint})
    assert any("earlier attempt" in p for p in again.problems)
    assert any("not quoted verbatim" in p for p in _check(proposal, ["window"], evidence="window is too long").problems)
    assert any("at least" in p for p in _check(proposal, ["window"], evidence="x").problems)
    rule = RecoveryRule(tunable={"paths": Bound(values=(["tests/test_x.py"],))})
    call = dict(CALL, method="source_review", parameters={"source_ids": ["a"]})
    sneaky = {"method": "source_review", "parameters": {"source_ids": ["a"], "paths": ["tests/test_x.py"]}}
    problems = _check(sneaky, ["paths"], rule=rule, call=call).problems
    assert any("protected" in p for p in problems)


def test_nested_parameters_diff_by_dotted_path():
    assert flatten({"bond": {"coupon": 0.05, "face": 100}, "y": 1}) == {"bond.coupon": 0.05, "bond.face": 100, "y": 1}
    assert param_diff({"bond": {"coupon": 0.05, "face": 100}}, {"bond": {"coupon": 0.04, "face": 100}}) == ("bond.coupon",)


# --- agents ---------------------------------------------------------------------------------------

def test_verdict_checks_and_sampling():
    state = {"plan": {"run_id": "r1", "objective": "o", "code_version": "s"}}
    card = default_card(state)
    good = {"verdict": "accept", "reasons": ["ok"], "criteria": [{"id": "objective", "satisfied": True, "finding": "f"}]}
    assert verdict_problems(good, card) == []
    assert verdict_problems(dict(good, criteria=[]), card)
    bad = dict(good, criteria=[{"id": "objective", "satisfied": False, "finding": "f"}])
    assert verdict_problems(bad, card) == ["validator accepted with an unsatisfied criterion"]
    assert not sampled("any", 0.0) and sampled("any", 1.0)
    assert [sampled(f"run{i}", 0.5) for i in range(20)] == [sampled(f"run{i}", 0.5) for i in range(20)]
    assert 0 < sum(sampled(f"run{i}", 0.5) for i in range(200)) < 200


# --- workflow review hook --------------------------------------------------------------------------

def _workflow(tmp_path, review):
    research = ResearchStore(tmp_path / "r.sqlite")
    return research, AutonomousWorkflow(RunStore(research), risk_registry(), max_workers=1, review=review)


@pytest.mark.parametrize("review,status", [(lambda s: None, "completed"), (lambda s: "drift", "escalated"),
                                            (lambda s: 1 / 0, "escalated"), (lambda s: "", "escalated")])
def test_review_hook_runs_before_completion(tmp_path, review, status):
    research, wf = _workflow(tmp_path, review)
    wf.submit(suite._vol("rv", 10))
    state = wf.run("rv")
    assert state["status"] == status
    if status == "escalated":
        assert state["report"] is None
        with pytest.raises(ValueError):
            wf.accept_reviewed("rv", reviewer=" ", notes="x")
        accepted = wf.accept_reviewed("rv", reviewer="walleva1715", notes="fine")
        assert accepted["status"] == "completed" and accepted["report"] is not None
        with pytest.raises(ValueError):
            wf.accept_reviewed("rv", reviewer="walleva1715", notes="again")
    research.close()


# --- ladder behaviors beyond the eval ---------------------------------------------------------------

def test_stack_without_recovery_policy_keeps_the_old_routing(tmp_path):
    from finquint.orchestration.host import Stack
    stack = Stack(tmp_path, approval_key=b"a" * 32, capability_key=b"c" * 32,
                  approval_policy=suite.APPROVAL_POLICY_PATH, registry=risk_registry())
    outcome = stack.run_research(suite._vol("old", 60))
    assert stack.ladder is None and outcome.route == "blocked" and outcome.final_run_id is None
    stack.close()


def test_handle_is_idempotent_and_children_do_not_count_for_the_breaker(tmp_path):
    stack = suite._stack(tmp_path, script=[{"set": {"window": 20}}])
    first = stack.run_research(suite._vol("idem", 60))
    again = stack.run_research(suite._vol("idem", 60))
    assert first.route == again.route == "recovered" and first.final_run_id == again.final_run_id == "idem-r1"
    rows = stack.db.execute("SELECT run_id FROM recovery_method_runs").fetchall()
    assert {r[0] for r in rows} == {"idem"}
    with pytest.raises(ValueError):
        stack.ladder.handle(stack.runs.load("idem-r1"))
    stack.close()


def test_validator_is_blind_to_the_repair_reasoning(tmp_path):
    validator = SimulatedValidator()
    stack = suite._stack(tmp_path, script=[{"set": {"window": 20}}], validator=validator)
    stack.run_research(suite._vol("blind", 60))
    reflection = stack.ladder.attempts("blind")[0]["reflection"]
    assert validator.seen and all(reflection["diagnosis"] not in text and reflection["differs_from_previous"] not in text
                                  and "hypothesis" not in text for text in validator.seen)
    stack.close()


def test_no_repairer_or_validator_escalates(tmp_path):
    stack = suite._stack(tmp_path, script=[{"set": {"window": 20}}])
    stack.ladder.repairer = None
    assert stack.run_research(suite._vol("norep", 60)).route == "escalated"
    stack.ladder.repairer, stack.ladder.validator = ModelCaller(SimulatedRepairer([{"set": {"window": 20}}])), None
    outcome = stack.run_research(suite._vol("noval", 60))
    assert outcome.route == "escalated" and stack.runs.load("noval-r1")["status"] == "escalated"
    stack.close()


def test_validator_skipping_a_criterion_escalates(tmp_path):
    stack = suite._stack(tmp_path, script=[{"set": {"window": 20}}], validator=SimulatedValidator(skip_criteria=True))
    card = suite._window_card("skip").__class__("skip", "run", "Report volatility",
                                               suite._window_card("skip").criteria[1:], "sha-recovery-eval")
    outcome = stack.run_research(suite._vol("skip", 60), card=card)
    assert outcome.route == "escalated"
    assert stack.ladder.reviews("skip-r1")[0]["problems"]
    stack.close()


def test_cost_cap_stops_the_loop(tmp_path):
    stack = suite._stack(tmp_path, script=[{"set": {"window": 45}}, {"set": {"window": 20}}])
    stack.ladder.pricing = lambda reply: 0.6
    outcome = stack.run_research(suite._vol("cap", 60))
    assert outcome.route == "escalated" and "cost cap" in outcome.reason
    assert len(stack.ladder.attempts("cap")) == 1
    stack.close()


def test_validator_model_must_match_policy(tmp_path):
    policy = replace(POLICY, validator_model="claude-opus-5-5")
    with pytest.raises(ValueError):
        suite._stack(tmp_path, policy=policy)


def test_invalid_known_fix_escalates_without_running(tmp_path):
    policy = replace(POLICY, known_fixes=(KnownFix("rolling_volatility", "gate:rolling_volatility_window",
                                                   {"parameters": {"window": "twenty"}}),))
    stack = suite._stack(tmp_path, policy=policy)
    outcome = stack.run_research(suite._vol("badfix", 60))
    assert outcome.route == "escalated" and stack.ladder.attempts("badfix")[0]["outcome"] == "guard_rejected"
    stack.close()


class _DownPlug:
    name, socket_version = "down", 1

    def __init__(self):
        from finquint.orchestration.models import Capabilities
        self.capabilities = Capabilities(context_window=100_000)

    def complete(self, request):
        from finquint.orchestration.models import ModelUnavailable
        raise ModelUnavailable("503")


def test_model_outages_escalate_and_never_accept(tmp_path):
    stack = suite._stack(tmp_path)
    stack.ladder.repairer = ModelCaller(_DownPlug())
    assert "unavailable" in stack.run_research(suite._vol("down1", 60)).reason
    stack.ladder.repairer = ModelCaller(SimulatedRepairer([{"set": {"window": 20}}]))
    stack.ladder.validator = ModelCaller(_DownPlug())
    outcome = stack.run_research(suite._vol("down2", 60))
    assert outcome.route == "escalated" and stack.runs.load("down2-r1")["status"] == "escalated"
    stack.close()


@pytest.mark.parametrize("decision,status", [("reject", "human_rejected"), ("revise", "human_revise")])
def test_human_reject_and_revise_close_the_escalation(tmp_path, decision, status):
    stack, root = suite.t_gaming_validator(str(tmp_path))
    request_id = stack.db.execute("SELECT request_id FROM recovery_runs WHERE root_run_id=?", (root,)).fetchone()[0]
    record = stack.approvals.decide(request_id, reviewer=suite.REVIEWER, decision=decision, notes="use 60 days")
    with pytest.raises(ValueError):
        stack.ladder.resolve("other", record.record_id)
    outcome = stack.ladder.resolve(root, record.record_id)
    assert outcome.route == "blocked" and stack.ladder.outcome(root).route == "blocked"
    assert stack.db.execute("SELECT status FROM recovery_runs WHERE root_run_id=?", (root,)).fetchone()[0] == status
    assert stack.runs.load("drift-r1")["status"] == "escalated"   # never ingested
    with pytest.raises(ValueError):
        stack.ladder.resolve(root, record.record_id)
    stack.close()


def test_escalation_packet_is_complete_for_the_reviewer(tmp_path):
    stack, root = suite.t_unfixable(str(tmp_path))
    request = stack.approvals.requests(run_id=root)[0]
    packet = json.loads(stack.db.execute("SELECT evidence FROM approval_requests WHERE request_id=?",
                                         (request.request_id,)).fetchone()[0])
    assert packet["original_call"]["parameters"]["window"] == 60 and packet["failure"]
    assert [a["outcome"] for a in packet["attempts"]] == ["failed", "failed"]
    assert all(a["reflection"]["evidence"] for a in packet["attempts"]) and packet["reason"]
    stack.close()


def test_schema_fault_records_the_calls_the_core_made(tmp_path):
    stack, root = suite.t_schema_fault(str(tmp_path))
    assert stack.ladder.outcome(root).model_calls == 2   # the reply plus the core's one repair
    assert stack.ladder.attempts(root)[0]["outcome"] == "model_escalated"
    stack.close()


# --- exit test --------------------------------------------------------------------------------------

def test_phase8_exit_matches_the_baseline():
    """Fails on regressions and on improvements: a human re-records with --write-baseline."""
    doc = suite.document()
    baseline = suite.load_baseline()
    assert doc["summary"] == baseline["summary"]
    assert doc["tasks"] == baseline["tasks"]
    s = doc["summary"]
    assert s["criteria_passed"] == s["criteria_total"]
    assert s["llm_calls_policy_path"] == 0 and s["gaming_accepted"] == 0
    assert s["fixable_recovered"] == s["fixable_total"]
