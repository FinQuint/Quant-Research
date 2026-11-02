import pytest

from finquint.orchestration import (
    ActionStatus, CapabilityAuthority, CapabilityError, CheckResult, GateDecision, GatePolicy,
    ProposedAction, decide, default_policy, fingerprint,
)

SECRET = b"k" * 32
PASSED = ActionStatus(CheckResult.PASSED, 0)


def action(action_type="run_backtest", target="dataset:prices_v7", **params):
    return ProposedAction(action_type, target, params, code_sha="abc123")


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.mark.parametrize("action_type, expected", [
    ("run_backtest", GateDecision.AUTO_CONTINUE),
    ("run_analysis", GateDecision.AUTO_CONTINUE),
    ("research", GateDecision.AUTO_CONTINUE),
    ("open_pr", GateDecision.AUTO_CONTINUE),
    ("merge_to_main", GateDecision.REQUEST_HUMAN),
    ("deploy", GateDecision.REQUEST_HUMAN),
    ("delete_production_data", GateDecision.REQUEST_HUMAN),
    ("escalate_authorization", GateDecision.BLOCK),
    ("anything_unknown", GateDecision.BLOCK),
])
def test_initial_policy_table(action_type, expected):
    assert decide(action(action_type), PASSED, default_policy()) is expected


@pytest.mark.parametrize("paths", [["tests/test_bond.py"], ["configs/agent_policy.yaml"],
                                   ["src/finquint/evaluation/checks.py"], ["tests"],
                                   ["docs/../tests/x.py"], ["./tests\\test_bond.py"], "tests/x.py", [3]])
def test_protected_or_unreadable_paths_block(paths):
    assert decide(action("open_pr", "repo:main", paths=paths), PASSED, default_policy()) is GateDecision.BLOCK


def test_protected_path_target_blocks_and_neighbours_do_not():
    policy = default_policy()
    assert decide(action("open_pr", "path:tests/test_x.py"), PASSED, policy) is GateDecision.BLOCK
    assert decide(action("open_pr", "repo:main", paths=["tests_extra/x.py", "src/finquint/risk/var.py"]),
                  PASSED, policy) is GateDecision.AUTO_CONTINUE


def test_block_wins_over_retry_and_human():
    status = ActionStatus(CheckResult.INFRA_ERROR, 0)
    assert decide(action("merge_to_main", "repo:main", paths=["tests/a.py"]), status, default_policy()) is GateDecision.BLOCK


def test_infra_errors_retry_before_check_failures_and_escalate_when_exhausted():
    policy = default_policy(default_max_retries=2)
    assert decide(action(), ActionStatus(CheckResult.INFRA_ERROR, 0), policy) is GateDecision.RETRY
    assert decide(action(), ActionStatus(CheckResult.INFRA_ERROR, 1), policy) is GateDecision.RETRY
    assert decide(action(), ActionStatus(CheckResult.INFRA_ERROR, 2), policy) is GateDecision.ESCALATE
    assert decide(action(), ActionStatus(CheckResult.FAILED, 0), policy) is GateDecision.BLOCK


def test_exhausted_caller_budget_escalates_whatever_the_policy_allows():
    policy = default_policy(default_max_retries=5)
    assert decide(action(), ActionStatus(CheckResult.INFRA_ERROR, 1, retries_exhausted=True), policy) \
        is GateDecision.ESCALATE
    assert decide(action(), ActionStatus(CheckResult.INFRA_ERROR, 1), policy) is GateDecision.RETRY
    assert decide(action(), ActionStatus(CheckResult.PASSED, 1, retries_exhausted=True), policy) \
        is GateDecision.AUTO_CONTINUE
    with pytest.raises(ValueError):
        ActionStatus(CheckResult.INFRA_ERROR, 0, retries_exhausted="yes")


def test_retry_limits_are_configurable_per_action():
    policy = default_policy(default_max_retries=0, retry_overrides={"research": 3})
    assert decide(action(), ActionStatus(CheckResult.INFRA_ERROR, 0), policy) is GateDecision.ESCALATE
    assert decide(action("research"), ActionStatus(CheckResult.INFRA_ERROR, 2), policy) is GateDecision.RETRY


def test_policy_rejects_inconsistent_configuration():
    with pytest.raises(ValueError):
        GatePolicy(allowed_actions={"a"}, human_required={"b"})
    with pytest.raises(ValueError):
        GatePolicy(allowed_actions={"a"}, default_max_retries=-1)
    with pytest.raises(ValueError):
        GatePolicy(allowed_actions={"a"}, protected_paths=("..",))
    policy = GatePolicy(allowed_actions=["a"], retry_overrides={"a": 1})
    with pytest.raises(TypeError):
        policy.retry_overrides["a"] = 5


def test_fingerprint_covers_semantic_payload_only():
    base = action(branch="feature/x")
    assert fingerprint(base) == fingerprint(action(branch="feature/x"))
    assert fingerprint(base) != fingerprint(action(branch="feature/y"))
    assert fingerprint(base) != fingerprint(ProposedAction("run_backtest", "dataset:prices_v7",
                                                           {"branch": "feature/x"}, "abc124"))
    # ActionStatus (retry state) is not part of the action, so retries cannot change it.
    assert len(fingerprint(base)) == 64


def test_action_and_status_validation():
    with pytest.raises(ValueError):
        ProposedAction("", "repo:main")
    with pytest.raises(ValueError):
        ProposedAction("deploy", "repo:main", {"x": float("nan")})
    with pytest.raises(ValueError):
        ActionStatus("passed", 0)
    with pytest.raises(ValueError):
        ActionStatus(CheckResult.PASSED, -1)
    assert ProposedAction.from_dict(action(a=1).to_dict()) == action(a=1)


def test_token_verifies_only_for_its_tool_fingerprint_and_lifetime():
    clock = Clock()
    authority = CapabilityAuthority(SECRET, ttl_seconds=600, clock=clock)
    a = action("open_pr", "repo:main")
    token = authority.mint("open_pr", fingerprint(a))
    authority.verify(token, tool="open_pr", action_fingerprint=fingerprint(a))
    with pytest.raises(CapabilityError, match="requires"):
        authority.verify(None, tool="open_pr", action_fingerprint=fingerprint(a))
    with pytest.raises(CapabilityError, match="different tool"):
        authority.verify(token, tool="deploy", action_fingerprint=fingerprint(a))
    with pytest.raises(CapabilityError, match="different action"):
        authority.verify(token, tool="open_pr", action_fingerprint=fingerprint(action("open_pr", "repo:dev")))
    clock.now += 600
    with pytest.raises(CapabilityError, match="expired"):
        authority.verify(token, tool="open_pr", action_fingerprint=fingerprint(a))


def test_tokens_cannot_be_forged_or_widened():
    authority = CapabilityAuthority(SECRET, ttl_seconds=600)
    a = action("open_pr", "repo:main")
    token = authority.mint("open_pr", fingerprint(a))
    from dataclasses import replace
    with pytest.raises(CapabilityError, match="signature"):
        authority.verify(replace(token, expires_at=token.expires_at + 10_000),
                         tool="open_pr", action_fingerprint=fingerprint(a))
    other = CapabilityAuthority(b"z" * 32, ttl_seconds=600)
    with pytest.raises(CapabilityError, match="signature"):
        other.verify(token, tool="open_pr", action_fingerprint=fingerprint(a))


def test_authority_configuration_and_grant():
    with pytest.raises(ValueError):
        CapabilityAuthority(b"short", ttl_seconds=600)
    with pytest.raises(ValueError):
        CapabilityAuthority(SECRET, ttl_seconds=0)
    authority = CapabilityAuthority(SECRET, ttl_seconds=600)
    a = action("open_pr", "repo:main")
    assert authority.grant(a, GateDecision.AUTO_CONTINUE).fingerprint == fingerprint(a)
    for decision in (GateDecision.REQUEST_HUMAN, GateDecision.RETRY, GateDecision.ESCALATE, GateDecision.BLOCK):
        assert authority.grant(a, decision) is None
