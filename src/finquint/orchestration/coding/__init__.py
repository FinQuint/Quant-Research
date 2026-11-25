"""The coding agent (roadmap Phase 10, ADR-023): new strategy templates only.

  policy       configs/coding_policy.json: limits and the import allowlist
  proposal     host-built module path, registration file and unified diff
  static       scope, AST and template-contract checks (nothing is executed)
  sandbox      Sandbox interface; SubprocessSandbox (temp dir, python -I, stripped env, timeout)
  conformance  the protected conformance suite, run inside the sandbox
  loop         CodingService: attempts, novelty, sandbox, blind review, PR branch, merge-gated load
  simulated    deterministic coding agent and reviewer for CI and the eval
"""
from .loop import CODING_ESCALATION, CodingOutcome, CodingService, NotMerged
from .policy import CodingPolicy
from .sandbox import Sandbox, SandboxResult, SubprocessSandbox
from .static import StaticCheck, TemplateSpec, check_proposal

__all__ = ["CODING_ESCALATION", "CodingOutcome", "CodingPolicy", "CodingService", "NotMerged", "Sandbox",
           "SandboxResult", "StaticCheck", "SubprocessSandbox", "TemplateSpec", "check_proposal"]
