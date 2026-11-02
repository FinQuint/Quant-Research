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
| 1 Safe side effects | Tool registry, idempotency ledger, outbox, capability tokens, `decide()` + tests | **Done** — merged to `main` as PR #14 (383f069) |
| 2 Protected evaluator | Branch protection, CODEOWNERS, lookahead/split invariants | **Done** — merged to `main` as PR #15 (f44815f) |
| 3 Approval path | Human gate, signed approvals, verification, expiry, branch-level pausing | **Done** — merged to `main` as PR #16 (df104b3) |
| 4 Eval & replay harness | Golden tasks, recorded traces, fault suite, tracing | **Exit test passes; not committed** — branch `feature/eval-harness` |
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
- **2026-11-01 — Phase 2 implementation choices.**
  - `orchestration/verify/paths.py` is the one source of protected paths
    (`DEFAULT_PROTECTED_PATHS`). It adds the enforcing code (`orchestration/gate`, `verify`),
    `.github`, CODEOWNERS and pytest config files. `.github/CODEOWNERS` (owner `@walleva1715`)
    must match it; a test enforces that.
  - Touched paths come from the action, not only what the agent declares: `params["patch"]`
    (unified diff) is parsed with hunk line counts, and both `-p0` and `-p1` readings are
    checked. Paths are compared case-insensitively; Windows trailing dots and spaces are
    stripped. Absolute, `..`, NUL or `:` paths, malformed patches, and a `diff_sha` that
    doesn't match the patch all fail closed (BLOCK).
  - `GuardedTools` checks protected paths for every non-read-only tool, before the token
    check, so a valid token cannot get past it. Turn it off only with `protected_paths=()`.
  - Lookahead invariant = truncation test: `strategy_factory(data)` must build the strategy
    from the data it is given. Orders up to each cutoff must be identical when later bars are
    removed. A strategy that isn't deterministic fails (cannot verify). `cutoff_count` is (tune).
  - The split is frozen by `SplitManifest.digest()`. Tuning must read only
    `[train_start, train_end]`. `judge_improvement` accepts a change only when its
    out-of-sample gain is more than `min_oos_improvement` (tune).
  - CI lists protected files changed on every PR. It fails only for `agent/*` head branches
    (a new convention for agent-authored PRs). Humans rely on CODEOWNERS review.
  - Adapters (`orchestration/adapters.py`) translate autonomous state into gate inputs and share
    the audit log. `AutonomousWorkflow` itself is unchanged until Phase 3.
- **2026-11-01 — Phase 3 implementation choices.**
  - `gate/approvals.py` `ApprovalService`: requests and HMAC-signed records in SQLite, using an
    approval key separate from the capability key. Records are immutable (SQLite triggers) and
    single-use (`approval_uses`). `verify()` re-checks signature, reviewer authority under the
    *current* policy, fingerprint, expiry and use, then mints the capability token.
  - Policy is `configs/approval_policy.json` (reviewers → roles, per-action role and approval
    count, `request_ttl_seconds` 24 h, `approval_ttl_seconds` 1 h, `on_expiry` escalate/block, all
    (tune)). An action with no rule cannot open a request (fail closed).
  - `gate/human_gate.py` `HumanGate`: `open()` is idempotent per (run, branch, fingerprint);
    `resume()` takes only `{"approval_record_id": ...}` and maps outcomes to Route
    EXECUTE/WAIT/REPLAN/BLOCK/ESCALATE.
  - `orchestration/scheduler.py` `Scheduler`: persisted branches, one action per branch per
    `tick()`, single-threaded and deterministic. Only a waiting branch pauses. Before an approved
    action runs, its checks and preconditions are evaluated again; any change → ESCALATE.
  - The reviewer CLI is `python -m finquint.orchestration.approve`, with the key in
    `FINQUINT_APPROVAL_KEY`. It is the only `decide()` entry point; never give the key to an
    agent process.
  - `AutonomousWorkflow(approvals=HumanGate)` is opt-in. With it, `decide()` is refused and
    `resume_with_approval()` / `expire_approvals()` are used instead. Without it, behavior is
    unchanged. `escalated` is a new terminal run status.
  - Known limit: `verify()` consumes the approval before the tool is called. A crash in between
    leaves the branch waiting with a used record. A second resume then BLOCKs (fail closed) and
    a new request is needed.
- **2026-11-01 — Phase 4 implementation choices.** (See `docs/EVALS.md`.)
  - Tracing has no OpenTelemetry dependency. `orchestration/tracing.py` `Tracer` keeps spans
    in memory; children inherit `run_id`/`task_id`/`code_sha`/`model`, and parents are tracked
    per thread. `SQLiteSpanStore` persists spans; `to_otlp_json()` emits the OTLP/JSON shape.
    `tracer=` is optional on `AutonomousWorkflow`, `Scheduler`, `GuardedTools` and
    `OutboxWorker`. Without it, behavior is unchanged.
  - The harness is `orchestration/evals/` (`harness.py`, `golden/`, `faults/`, `replay/`).
    13 tasks: 1 research, 2 analysis, 3 coding, 7 fault (the spec's five faults; API down and
    crash mid-merge each have two variants). Agents are the deterministic reference agents.
  - Scores are criteria passed / total, plus metrics. Regression metrics must not rise:
    criteria_missed, citation_failures, safety_failures, duplicate_side_effects,
    untagged_spans. Tracked metrics (human_interrupts, tool_calls, spans) are only reported.
    The pytest exit test fails on regressions *and* on improvements, so the baseline is kept
    current.
  - Replay records tool handlers, reconcile checks and method executions. Gates, invariants,
    ledger, approvals and scheduler always run for real. Traces store input digests, outputs
    and outcome (facts plus audit event types). They hold no random IDs or timestamps, so they
    are deterministic.
  - `src/finquint/orchestration/evals` is a protected path (added to `DEFAULT_PROTECTED_PATHS`
    and CODEOWNERS). Baselines and traces are re-recorded by a human with
    `--write-baseline --record-traces`.
  - Honest baseline: `fault_contradictory_sources` scores 0.333 (`known_gap`). No
    evidence-conflict check exists; the spec wants ESCALATE. "Stale index" is tested as a
    pinned method changing after planning, since the retrieval index arrives in Phase 5.
  - Known limit: the baseline and trace JSON files are read from the source tree. A non-editable
    `pip install` would need package-data config in `pyproject.toml`, which is protected and
    was left unchanged.

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
- 2026-11-01: Phase 1 PR opened (user opened it in browser). GitHub CLI installed and logged in by
  user; restarting session to load it and the GitHub connector, which failed with "Authorization
  header is badly formatted". Next: confirm `gh auth status` and connector tools, then start
  Phase 2 on `feature/protected-evaluator` branched off `feature/safe-side-effects` (Phase 1 not
  yet merged; retarget the PR to `main` once it lands). Branch protection needs repo admin on GitHub.
- 2026-11-01: `gh` 2.103.3 works, logged in as `walleva1715` with ADMIN on FinQuint/Quant-Research.
  If `gh` is not found, the session PATH is stale: use `& "C:\Program Files\GitHub CLI\gh.exe"`.
  GitHub connector still fails to connect. PR #14 (Phase 1) merged; `main` has no branch
  protection yet. Created `feature/protected-evaluator` from `main`. Suite green.
- 2026-11-01: Phase 2 code in `orchestration/verify/` (paths, invariants, CLI), wired into
  `decide()`, `GuardedTools` and `adapters.status_for_invariants`; `.github/CODEOWNERS`; CI
  protected-path step. `tests/test_orchestration_protected.py`: 293 passed (243 + 50). Not
  committed.
- 2026-11-01: Branch protection applied to `main` (option 1): CI checks `Python 3.10/3.11/3.12`
  must pass on an up-to-date branch, 1 approving review from a code owner, stale reviews
  dismissed, conversations resolved, no force-push or deletion. `enforce_admins` is false so
  the sole owner can merge their own PRs; switch it on once a second reviewer exists.
  Phase 2 committed and PR opened. Next: review/merge PR, then Phase 3 (approval path).
- 2026-11-01: PR #15 (Phase 2) merged. Phase 3 implemented on `feature/approval-path`:
  `gate/approvals.py`, `gate/human_gate.py`, `scheduler.py`, `approve.py` CLI,
  `configs/approval_policy.json`, opt-in hook in `autonomous/runtime.py`.
  `tests/test_orchestration_approvals.py`: 321 passed (293 + 28). Committed (037838f), pushed,
  PR #16 opened. Next: CI + review/merge PR #16, then Phase 4 (eval & replay harness).
- 2026-11-01: PR #16 merged (df104b3; CI green, merged with admin bypass, no code-owner review).
  Phase 4 on `feature/eval-harness`: `orchestration/tracing.py`, `orchestration/evals/`
  (13 golden/fault tasks, baseline, 13 replay traces, CLI), `docs/EVALS.md`,
  `tests/test_orchestration_evals.py`. Suite: 341 passed (321 + 20). Not committed. Next:
  review, commit, PR; then Phase 5 (context layer). Its exit compares against this baseline.
