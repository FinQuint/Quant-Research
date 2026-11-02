"""Pure gate: (action, status, policy) -> one of five outcomes (ADR-001, ADR-002).

The gate only routes. Enforcement lives in the tool layer (ADR-003): a side-effecting
tool refuses to run without a capability token bound to the action's fingerprint.
"""
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from types import MappingProxyType
from typing import Mapping

from finquint.autonomous.models import canonical

from ..verify.paths import DEFAULT_PROTECTED_PATHS, PatchError, action_paths, is_protected, normalize_path


class GateDecision(str, Enum):
    AUTO_CONTINUE = "auto_continue"
    RETRY = "retry"
    REQUEST_HUMAN = "request_human"
    ESCALATE = "escalate"   # needs judgment: supervisor first, then human
    BLOCK = "block"         # forbidden; no one can override


class CheckResult(str, Enum):
    PASSED = "passed"
    FAILED = "failed"            # real failure of a mandatory check
    INFRA_ERROR = "infra_error"  # check could not run (timeout, API down)


@dataclass(frozen=True)
class ProposedAction:
    action_type: str
    target: str                  # e.g. "repo:main", "dataset:prices_v7", "path:tests/x.py"
    params: dict = field(default_factory=dict)
    code_sha: str = ""
    diff_sha: str | None = None  # hash of the exact patch, if any

    def __post_init__(self):
        if not isinstance(self.action_type, str) or not self.action_type.strip():
            raise ValueError("action_type is required")
        if not isinstance(self.target, str) or not self.target.strip():
            raise ValueError("target is required")
        if not isinstance(self.params, dict):
            raise ValueError("params must be a JSON object")
        if not isinstance(self.code_sha, str) or not (self.diff_sha is None or isinstance(self.diff_sha, str)):
            raise ValueError("code_sha and diff_sha must be strings")
        canonical(self.params)  # JSON only; rejects NaN/Infinity at any depth

    def to_dict(self):
        return {"action_type": self.action_type, "target": self.target, "params": self.params,
                "code_sha": self.code_sha, "diff_sha": self.diff_sha}

    @classmethod
    def from_dict(cls, data):
        return cls(data["action_type"], data["target"], data["params"], data["code_sha"], data["diff_sha"])


@dataclass(frozen=True)
class ActionStatus:
    checks: CheckResult
    retry_count: int
    retries_exhausted: bool = False  # the caller's own attempt budget is spent

    def __post_init__(self):
        if not isinstance(self.checks, CheckResult):
            raise ValueError("checks must be a CheckResult")
        if type(self.retry_count) is not int or self.retry_count < 0:
            raise ValueError("retry_count must be a non-negative integer")
        if type(self.retries_exhausted) is not bool:
            raise ValueError("retries_exhausted must be boolean")


@dataclass(frozen=True)
class GatePolicy:
    """Deterministic, versioned policy. Retry counts are (tune) values: pass them in."""
    allowed_actions: frozenset
    human_required: frozenset = frozenset()
    protected_paths: tuple = ()
    default_max_retries: int = 2
    retry_overrides: Mapping = field(default_factory=dict)
    version: str = "1"

    def __post_init__(self):
        allowed, human = frozenset(self.allowed_actions), frozenset(self.human_required)
        if not human <= allowed:
            raise ValueError("human_required actions must also be allowed")
        protected = tuple(normalize_path(p) for p in self.protected_paths)
        if any(not p for p in protected):
            raise ValueError("protected paths must be non-empty relative paths")
        retries = dict(self.retry_overrides)
        for value in (self.default_max_retries, *retries.values()):
            if type(value) is not int or value < 0:
                raise ValueError("retry limits must be non-negative integers")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("policy version is required")
        object.__setattr__(self, "allowed_actions", allowed)
        object.__setattr__(self, "human_required", human)
        object.__setattr__(self, "protected_paths", protected)
        object.__setattr__(self, "retry_overrides", MappingProxyType(retries))

    def max_retries(self, action_type):
        return self.retry_overrides.get(action_type, self.default_max_retries)

    def touched_paths(self, action):
        """Paths an action touches: a `path:` target, params["paths"], and every file in
        params["patch"]. None = malformed (including a patch that does not match diff_sha)."""
        try:
            return [normalize_path(p) for p in action_paths(action)]
        except PatchError:
            return None

    def violates_invariant(self, action):
        paths = self.touched_paths(action)
        if paths is None or any(p is None for p in paths):
            return True  # fail closed on anything we cannot read
        return any(is_protected(p, self.protected_paths) for p in paths)


def default_policy(*, default_max_retries=2, retry_overrides=None):
    """The spec's initial policy table, with the Phase 2 protected paths."""
    return GatePolicy(
        allowed_actions={"run_backtest", "run_analysis", "research", "open_pr",
                         "merge_to_main", "deploy", "delete_production_data"},
        human_required={"merge_to_main", "deploy", "delete_production_data"},
        protected_paths=DEFAULT_PROTECTED_PATHS,
        default_max_retries=default_max_retries,
        retry_overrides=retry_overrides or {},
    )


def decide(a: ProposedAction, s: ActionStatus, policy: GatePolicy) -> GateDecision:
    if a.action_type not in policy.allowed_actions:
        return GateDecision.BLOCK               # unknown capability: fail closed
    if policy.violates_invariant(a):            # e.g. touches protected eval paths
        return GateDecision.BLOCK
    if s.checks is CheckResult.INFRA_ERROR:     # classify infra errors before check failures
        # The policy can stop retries sooner than the caller's budget, never later.
        if not s.retries_exhausted and s.retry_count < policy.max_retries(a.action_type):
            return GateDecision.RETRY
        return GateDecision.ESCALATE
    if s.checks is CheckResult.FAILED:
        return GateDecision.BLOCK               # this action only; the branch may revise
    if a.action_type in policy.human_required:
        return GateDecision.REQUEST_HUMAN
    return GateDecision.AUTO_CONTINUE


def fingerprint(a: ProposedAction) -> str:
    """Hash of the semantic payload only; retry state never changes it."""
    return sha256(canonical(a.to_dict()).encode()).hexdigest()
