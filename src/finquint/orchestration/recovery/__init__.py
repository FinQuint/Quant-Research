"""Failure recovery ladder (roadmap Phase 8, ADR-020).

Policy first: an LLM is never called for work the policy path completes. A failed run is triaged
deterministically; only fixable failures on a method with a recovery rule reach the repair agent,
whose proposals are checked by guards and the unchanged hard gates, then accepted only by a blind
validator agent. Everything else goes to a human through signed approvals.
"""
from .agents import (REPAIR_SCHEMA, REPAIR_SYSTEM, VALIDATOR_SCHEMA, VALIDATOR_SYSTEM, default_card,
                     repair_request, sampled, validator_request, verdict_problems)
from .guards import GuardResult, call_fingerprint, check_proposal, flatten, param_diff
from .ladder import ESCALATION_ACTION, LadderOutcome, RecoveryLadder, child_request
from .policy import CONFIG_PATH, Bound, KnownFix, RecoveryPolicy, RecoveryRule
from .triage import TIERS, Triage, failure_signature, triage

__all__ = ["CONFIG_PATH", "ESCALATION_ACTION", "REPAIR_SCHEMA", "REPAIR_SYSTEM", "TIERS", "VALIDATOR_SCHEMA",
           "VALIDATOR_SYSTEM", "Bound", "GuardResult", "KnownFix", "LadderOutcome", "RecoveryLadder",
           "RecoveryPolicy", "RecoveryRule", "Triage", "call_fingerprint", "check_proposal", "child_request",
           "default_card", "failure_signature", "flatten", "param_diff", "repair_request", "sampled", "triage",
           "validator_request", "verdict_problems"]
