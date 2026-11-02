# FinQuint Quant-Research — guidance for Claude

## What this repo is
Composable quant research pipelines and fixed-income analytics (`src/finquint/`), plus a governed
agent stack: research governance (10A, `research/`), specialist agents (10B, `agents/`),
evaluation (10C, `evaluation/`), and the autonomous orchestrator from PR #13 (`autonomous/`,
see `docs/AUTONOMOUS_WORKFLOW.md`).

## Current goal
Build the reliable autonomous agentic workflow defined in **`docs/AGENTIC_WORKFLOW_SPEC.md`**
(repo copy of the live spec: https://claude.ai/artifact/A8HdytgAaEqZrKU8o1dijF). Read the spec
before any orchestration work. Core rule: *agents propose; deterministic code verifies, gates, and
executes.* No live trading; never change the numerical algorithms.

## Roadmap status
| Phase | Scope | Status |
|---|---|---|
| 1 Safe side effects | Tool registry, idempotency ledger, outbox, capability tokens, `decide()` + tests | **Exit tests pass; pushed, PR into `main` awaiting review** — branch `feature/safe-side-effects` |
| 2 Protected evaluator | Branch protection, CODEOWNERS, lookahead/split invariants | Not started |
| 3 Approval path | Human gate, signed approvals, verification, expiry, branch-level pausing | Not started |
| 4 Eval & replay harness | Golden tasks, recorded traces, fault suite, tracing | Not started |
| 5 Context layer | Task card, builder, retrieval, health triggers, rebuild | Not started |
| 6 Knowledge lifecycle & number provenance | Re-indexing, tiers/promotion, placeholder renderer | Not started |

Each phase is done only when its **exit test** in the spec passes. Update this table when a phase lands.

## Decisions that refine the spec (fold into a new ADR in the spec when reviewed)
- **2026-11-01 — Build on PR #13, not LangGraph/Postgres (yet).** Phase 1 extends the existing
  explicit state machine in `finquint.autonomous` on SQLite (sharing `ResearchStore`). Ledger and
  outbox sit behind a storage interface so a Postgres backend (ADR-004) or LangGraph/Temporal can
  plug in later. No new runtime dependencies without discussion.
- New orchestration modules go under `src/finquint/orchestration/`, following the spec's
  "Module layout" tree (`gate/decide.py`, `tools/registry.py`, `tools/ledger.py`,
  `tools/guarded.py`, `gate/capabilities.py`), wired into `finquint.autonomous` via adapters.
- **2026-11-01 — Phase 1 implementation choices.**
  - Enums use `(str, Enum)`, not the spec's `StrEnum`, because CI includes Python 3.10.
  - Every non-read-only tool needs a token, AUTO_CONTINUE actions included
    (`CapabilityAuthority.grant()` mints one only for AUTO_CONTINUE; approvals mint in Phase 3).
  - The idempotency key hashes the canonical JSON list `[run_id, fingerprint, attempt_scope]`, so
    fields cannot run together.
  - Side-effect classes are `read_only`, `idempotent_write` (ledger) and `external` (ledger + outbox).
  - An external dispatch that was interrupted or raised is settled by the tool's optional
    `reconcile()`. With no `reconcile()` it becomes `in_doubt` for a human and is never re-sent.
  - Default protected paths are `tests`, `configs`, `src/finquint/evaluation` and
    `src/finquint/backtesting`. Phase 2 refines them.
  - Only one `OutboxWorker` runs per ledger. `recover()` runs only after the old worker has
    stopped; this is documented, not enforced.
  - Refines the spec's `decide()`: `ActionStatus.retries_exhausted` (default False). When it is
    true, an INFRA_ERROR always escalates, so the policy's retry limit can stop retries sooner
    than the plan's budget, never later. The field is not part of the fingerprint. Fold this
    into an ADR when the spec is reviewed.
  - Adapters (`orchestration/adapters.py`) translate autonomous state into gate inputs and share
    the audit log. `AutonomousWorkflow` itself is unchanged until Phase 3.

## Rules
- Work locally on a feature branch, one roadmap phase per branch/PR into `main`. Push only when asked.
- Additive changes only: existing analytics, `QuantPipeline`, 10A/10B/10C behavior and their tests
  must keep passing unchanged.
- Never weaken or edit existing tests, `configs/*_policy.yaml`, or evaluation gates to make
  something pass (ADR-005). New behavior gets new tests.
- Policy, gating and budgets are deterministic code, never LLM decisions (ADR-001).
- Values marked **(tune)** in the spec stay configurable, not hard-coded.
- Design changes: add a superseding ADR to the spec; never rewrite an accepted ADR.

## Commands (Windows PowerShell, repo root)
```powershell
$env:PYTHONPATH = "src"          # another finquint checkout may be installed on this machine
python -m pytest                 # full suite; baseline after PR #13 = 198 passed
python examples/autonomous_research.py
```
CI runs Python 3.10–3.12 (`.github/workflows/tests.yml`).

## Session handoff
At the end of a session, update "Roadmap status" and "Decisions" above, and add a line below.
The Claude project "Agentic Workflow" keeps a mirror in `claude/session-governance.md`.

- 2026-11-01: Spec copied into repo; local synced to `main` (7b5d87c); Phase 1 approach decided; next: implement Phase 1.
- 2026-11-01: Phase 1 implemented in `src/finquint/orchestration/` plus
  `tests/test_orchestration_{gate,side_effects}.py`. Suite: 240 passed (198 + 42). Not committed.
  Next: review, commit, PR; then Phase 2.
- 2026-11-01: `status_for_task` now maps a failed task with a RetryableError to INFRA_ERROR, so
  the gate escalates. Suite: 242 passed.
- 2026-11-01: Retry budgets reconciled (option 1). `status_for_task(task, max_attempts=...)` marks
  exhausted tasks; `decide()` escalates on them whatever the policy allows. Suite: 243 passed. Committed and pushed for PR.
