> Repo copy of the Claude Doc spec (v0.1, 2026-10-30). Live doc: https://claude.ai/artifact/A8HdytgAaEqZrKU8o1dijF
> Implementation decisions from Phases 1–6 (recorded in `CLAUDE.md`, "Decisions") are folded in as ADR-009 to ADR-018 (v0.2, proposed). `CLAUDE.md` keeps the detailed notes. v0.4 adds the failure recovery ladder (ADR-020, roadmap Phase 8); v0.5 folds the Phase 7 real-model decisions into ADR-021.

# FinQuint Reliable Agentic Workflow — Architecture Spec

Oct 30, 2026 · @Yunan Xie

## Document control

This is the single source of truth for the FinQuint orchestration architecture; version 0.5, draft for review.

| Field | Value |
| --- | --- |
| Version | 0.5 (draft) |
| Supersedes | ChatGPT design thread "Autonomous Workflow Architecture" (HITL gate, context management, dynamic knowledge) |
| Owner | Project lead |
| Review cadence | Every architecture change, and monthly while in build |

How to maintain this doc:

- Every design decision lives in one ADR in section 4. Change a decision by adding a new ADR that supersedes the old one; never edit an accepted ADR's decision text.
- Code in this doc is reference code. The repository is authoritative once a module exists; link to it instead of duplicating it.
- Thresholds and numbers are marked **(tune)** until an eval in section 7 justifies them.
- Log every version in the changelog at the end.

## Goal and scope

**Goal:** FinQuint runs long-horizon quant research autonomously for days (research, backtests, analysis, and self-improving code) while every result stays reproducible and no irreversible action happens without verified authorization.

Default behavior is autonomous. A human is pulled in only for protected operations, scope changes, and high-impact decisions the system cannot resolve from evidence.

**In scope**

- Deterministic orchestration (LangGraph) around specialist agents: Planner, Researcher, Coding Agent, Supervisor, Analyst.
- Human Intervention Gate and authorization path.
- Context and knowledge management for long runs.
- Reliability controls: idempotency, budgets, verification, evals, observability.

**Non-goals**

- Changing the numerical algorithms themselves.
- Live trading or order execution. Any future execution path needs its own spec.
- An LLM agent that decides policy, approvals, or context budgets.

**Success criteria** (targets marked tune until measured)

| Criterion | Target | Measured by |
| --- | --- | --- |
| Unattended run length | 72 h without human input on routine work (tune) | Run logs |
| Unauthorized protected actions | 0 | Audit log vs approval records |
| Duplicate side effects after crash or resume | 0 | Idempotency ledger, fault-injection evals |
| Reported numbers traceable to an artifact | 100% | Number-provenance check |
| Agent edits to protected evaluation files | 0 merged | Repo protection + audit |
| Human interrupts per run | Only policy-listed actions plus escalations | Gate decision log |
| LLM calls on tasks the policy path completes | 0 | Model call log vs run outcomes |
| Repairs accepted that change the question (gate gaming) | 0 | Recovery fault suite |

## Architecture overview

Agents propose; deterministic code verifies, gates, and executes.

_(Control-path diagram: see the live doc.)_

Agents never touch side effects directly. Outputs are verified, then gated; only guarded tools act, and they check a capability token and the idempotency ledger. RETRY goes back through the scheduler, and results land as artifacts the context builder reads next.

## Architecture decision records

Twenty-one decisions define the system; all are Proposed until reviewed. Supersede an ADR by adding a new row and setting the old one to Superseded. ADR-001 to ADR-008 are the original design; ADR-009 to ADR-021 record what the build phases decided (dates are when each was decided).

| ADR | Decision | Why | Status |
| --- | --- | --- | --- |
| ADR-001 | Policy, gating, and context budgeting are deterministic code, not LLM agents | Auditable, testable, cannot be talked out of a rule | Proposed |
| ADR-002 | Gate has five outcomes: AUTO\_CONTINUE, RETRY, REQUEST\_HUMAN, ESCALATE, BLOCK | Separates "needs judgment" (ESCALATE) from "forbidden" (BLOCK) | Proposed |
| ADR-003 | Enforcement lives in the tool layer via short-lived signed capability tokens; graph gate is advisory routing | No graph path, prompt injection, or tool substitution can bypass approval | Proposed |
| ADR-004 | Every side-effecting call is its own node with an idempotency key in a Postgres ledger; outbox for merges, deploys, writes | LangGraph replays a node on resume; duplicates must be impossible | Superseded by ADR-009 (storage and runtime only; the idempotency and outbox rules stand) |
| ADR-005 | Tests, backtest harness, acceptance criteria, and data splits are read-only to agents | Prevents the coding agent from "passing" by changing the evaluator | Proposed |
| ADR-006 | Context is rebuilt from a pinned task card plus authoritative artifacts, not chained summaries | Summaries of summaries lose and distort facts | Proposed |
| ADR-007 | LLMs never emit metrics; outputs cite artifact IDs and a renderer inserts numbers | Eliminates hallucinated results | Proposed |
| ADR-008 | Policy rules live in a policy engine (Cedar or OPA), versioned, changes require approval | Policy changes become reviewable diffs | Superseded by ADR-013 |
| ADR-009 | (2026-11-01, supersedes ADR-004's storage and runtime) Orchestration extends the explicit state machine in `finquint.autonomous` (PR #13) on SQLite, sharing `ResearchStore`, instead of LangGraph and Postgres. Ledger, outbox, approvals, evidence and metric stores sit behind storage interfaces, so Postgres, LangGraph or Temporal can plug in later. The idempotency key hashes the canonical JSON list `[run_id, fingerprint, attempt_scope]`. One `OutboxWorker` per ledger; `recover()` runs only after the old worker stops (documented, not enforced). No new runtime dependencies without discussion | Reuses tested code and keeps CI dependency-free; the interfaces keep the original target reachable | Proposed |
| ADR-010 | (2026-11-01, refines ADR-003/004) Side-effect classes are `read_only`, `idempotent_write` (ledger) and `external` (ledger + outbox). Every non-read-only tool needs a capability token, AUTO\_CONTINUE included. An interrupted or failed external dispatch is settled by the tool's optional `reconcile()`; without one it becomes `in_doubt` for a human and is never re-sent | An unknown outcome must never become a duplicate side effect | Proposed |
| ADR-011 | (2026-11-01, refines the gate) `ActionStatus.retries_exhausted` (default false, not fingerprinted): when true, an INFRA\_ERROR always escalates, so the policy's retry limit can stop retries sooner than the plan's budget, never later. A failed task with a `RetryableError` maps to INFRA\_ERROR. Enums use `(str, Enum)` because CI includes Python 3.10 | Two retry budgets must not let a run retry past either one | Proposed |
| ADR-012 | (2026-11-01, implements ADR-005) `orchestration/verify/paths.py` is the one list of protected paths (tests, configs, evaluation, backtesting, the gate, verify, evals and knowledge code, `.github`, CODEOWNERS, pytest config); CODEOWNERS must match it (test-enforced). Touched paths come from the action's patch, not only the agent's claim; malformed or ambiguous paths fail closed (BLOCK). `GuardedTools` checks paths before the token, so a valid token cannot get past it. Lookahead = truncation test; the split is frozen by `SplitManifest.digest()`; a change is accepted only on out-of-sample gain above `min_oos_improvement` (tune). CI fails protected-file changes on `agent/*` branches; humans rely on CODEOWNERS review | The evaluator must be out of reach of the agent being evaluated, whatever it declares | Proposed |
| ADR-013 | (2026-11-01, supersedes ADR-008) Policy lives in versioned files under the protected `configs/` path (`approval_policy.json`, `context_policy.json`), read by deterministic code; a policy change is a reviewed diff under CODEOWNERS. No Cedar or OPA engine yet. `ApprovalService` stores HMAC-signed, immutable, single-use approval records in SQLite, with a key separate from the capability key and held only by the reviewer CLI (`python -m finquint.orchestration.approve`). `verify()` re-checks signature, reviewer authority under the current policy, fingerprint, expiry and use, then mints the token. An action with no rule cannot open a request. TTLs and approval counts are (tune). Known limit: a crash between `verify()` and the tool call leaves a used record; resume then BLOCKs and a new request is needed | Same reviewable-diff property as ADR-008 without a new dependency; fails closed throughout | Proposed |
| ADR-014 | (2026-11-01) The scheduler runs persisted branches, one action per branch per tick, single-threaded and deterministic; only a waiting branch pauses. An approved action's checks and preconditions are re-evaluated before it runs, and any change escalates. `escalated` is a terminal run status. The human gate is opt-in on `AutonomousWorkflow`; without it behavior is unchanged | An approval must bind to the state it was given in, and other branches keep working meanwhile | Proposed |
| ADR-015 | (2026-11-01) Tracing is in-process (`orchestration/tracing.py`), persisted to SQLite and exportable as OTLP/JSON, with no OpenTelemetry dependency. The eval harness (`orchestration/evals/`, protected) runs golden and fault tasks with deterministic reference agents. Replay records only tool handlers, reconcile checks and method executions; gates, invariants, ledger, approvals and scheduler always run for real. Regression metrics must not rise, and the test also fails on improvements so a human re-records the baseline. Known gaps are recorded in the baseline, not hidden | Evals cannot regress silently, and traces compare byte for byte | Proposed |
| ADR-016 | (2026-11-04) Agents reach models only through the model socket (`orchestration/models/`), never a vendor SDK. A plug implements `complete(ModelRequest) -> ModelReply`, declares capabilities and a socket version, and must pass the conformance suite. Schema checks, one repair retry then ESCALATE, fallback order and error mapping run in the core, so every model gets the same safety behavior; a full outage surfaces as INFRA\_ERROR. CI gates on the deterministic simulated plug; real-model plugs are optional installs with per-model baselines. Token budgets are percentages of the plug's window, from an exact counter or a calibrated estimate (tune) | The model choice must not change safety behavior, and CI stays deterministic | Proposed |
| ADR-017 | (2026-11-04, implements ADR-006) The context layer stores evidence in SQLite with lexical retrieval behind an `EvidenceStore` interface; no embeddings yet. The task card holds immutable budget limits; budget left goes in the volatile part of the request, so the card stays byte-identical. Evidence with the same claim key but different values escalates; provisional evidence cannot be cited. Thresholds come from sweeps over long-run tasks, and `context_policy.json` must match the recorded picks. Re-indexing runs on explicit events (new code SHA, verified experiment, dataset version change), with stale marks from content hashes and old versions kept; the code index uses `ast` per SHA | Context stays rebuildable and auditable, and thresholds are measured, not guessed | Proposed |
| ADR-018 | (2026-11-04, implements ADR-007) Reports cite numbers as `{{metric:artifact_id:field}}` placeholders resolved through a read-only `MetricStore` that serves only verified artifacts (completed runs, verified experiments, current trusted evidence). Placeholders may format, never compute; a derived number is stored as its own artifact first. Any other digit run in a final output fails closed, except request tags, ISO dates and verbatim quotes from an input. Promotion from provisional to verified is a deterministic verifier (every number rendered, every citation authoritative); demotion is automatic when a source is superseded, and a promoted version is never rewritten. No human or model in the loop | Every number in a report traces to a verified artifact, or the report escalates | Proposed |
| ADR-019 | (2026-11-15) One host `Stack` (`orchestration/host.py`) wires every layer onto one run database: signed approvals, gate, ledger and outbox, scheduler, tracing, the knowledge hook, metric stores and reports through the model socket. It holds no state beyond the database, so a crashed host is replaced by a new `Stack` that runs outbox `recover()` first. Failed research runs are routed by `decide()` and every route is audited (`host_run_routed`). The success criteria are measured by a scorer that reads only durable state (the run database and the remote system's own delivery log), never the driver's counters, and the scorer has tamper tests | The criteria hold for the composed system, not only per layer, and the measurement cannot be talked into a pass | Proposed |
| ADR-020 | (2026-11-23) Failures climb a recovery ladder; an LLM is never called for work the policy path completes. A deterministic triage table (`configs/recovery_policy.json`) sends each failure to one tier: transient errors get blind retries (ADR-011) and then a human; forbidden actions BLOCK; only fixable failures (hard gate failed, input rejected, method error) on a method with a recovery rule reach the repair agent. The repair agent proposes a revised call (parameters within declared bounds, or a swap to a listed alternative method), never code, together with a structured reflection; deterministic guards (bounds, frozen parameters, novelty against every earlier attempt, reflection consistent with the actual diff, invariants) and the unchanged hard gates check it. Repair attempts are 2 per failure (tune). A repaired result is accepted only after a blind validator agent, which sees the task card, the original and final call and the result but none of the repair agent's reasoning, accepts it; it can reject or escalate, never override a hard gate. It also audits a deterministic sample of policy-path successes (rate (tune), default 0). Everything else goes to a human with an escalation packet through signed approvals. Accepted repairs become provisional lessons; a human can turn a repeated lesson into a deterministic known-fix rule | Agents take over only where policy cannot decide; every agent proposal is verified by code and by an independent check, and the policy path absorbs repeated fixes over time | Proposed |
| ADR-021 | (2026-11-15, Phase 7; refines ADR-016 to ADR-019) Real models plug into the socket as optional installs. `ClaudePlug` defaults (all (tune)): `claude-opus-5-5`, effort high, window capped at 200 000 tokens because budgets are a share of the window, thinking headroom added to `max_tokens`; SYSTEM segments become the system prompt with one cache breakpoint on the last stable segment; 408/409/429/5xx and connection errors are `ModelUnavailable`, other 4xx and a refusal are `ModelRefused`; server-side refusal fallbacks are off; exact token counting is opt-in. Real-model runs are demos (`orchestration/demos/`), never CI gates: replies are recorded by request digest with a normalized-key fallback (long decimals to 12 significant digits) for last-digit float differences across Python versions, and CI replays them offline. Fixes measured on real models: the context switches `prioritize_uncited` and `escalate_identical_rebuild` are on in `context_policy.json` (Phase 5 baseline re-recorded); every report uses one shared prompt (`REPORT_SYSTEM`: write non-metric numbers in words); the numeral check matches request identifiers ignoring case (identifiers always contain a letter); the citation check resolves a tag-form version `vN` to `N` only when no exact `vN` exists. The soak checks fault reports by ID and allows 0 routine report escalations; "human input on routine work" means an approval request for, or a recovery of, a research run (approvals of policy-listed merges are expected). A dataset version change also demotes verified claims whose supports, transitively, rest on old-version evidence | Every model meets the same safety behavior, recorded evidence replays deterministically, and each fix came from a measured real-model failure without letting an untraced number or an unverified citation through | Proposed |

## Human Intervention Gate

The gate is a pure function from (action, status, policy) to one of five outcomes; it routes the graph, while the tool layer enforces it (ADR-003).

**Fixes over the ChatGPT draft**

- Classify infrastructure errors before check failures. The draft blocked on `mandatory_checks_passed=False` before ever reaching the retry branch, so a flaky data API was blocked, not retried.
- Retry exhaustion returns ESCALATE, matching the config's `on_retry_exhaustion: escalate`. The draft returned BLOCK.
- Fingerprint only the semantic payload. The draft hashed `retry_count` and `transient_error`, so the fingerprint changed between attempts.
- Support the "Request revision" decision the UI offered.
- Resume carries only an approval record ID, never a bare "approve" string.

**Reference implementation** (`orchestration/gate/decide.py`)

```python
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
import json

class GateDecision(StrEnum):
    AUTO_CONTINUE = "auto_continue"
    RETRY = "retry"
    REQUEST_HUMAN = "request_human"
    ESCALATE = "escalate"   # needs judgment: supervisor first, then human
    BLOCK = "block"         # forbidden; no one can override

class CheckResult(StrEnum):
    PASSED = "passed"
    FAILED = "failed"            # real failure of a mandatory check
    INFRA_ERROR = "infra_error"  # check could not run (timeout, API down)

@dataclass(frozen=True)
class ProposedAction:
    action_type: str
    target: str                  # e.g. "repo:main", "dataset:prices_v7"
    params: dict = field(default_factory=dict)
    code_sha: str = ""
    diff_sha: str | None = None  # hash of the exact patch, if any

@dataclass(frozen=True)
class ActionStatus:
    checks: CheckResult
    retry_count: int

def decide(a: ProposedAction, s: ActionStatus, policy) -> GateDecision:
    if a.action_type not in policy.allowed_actions:
        return GateDecision.BLOCK               # unknown capability: fail closed
    if policy.violates_invariant(a):            # e.g. touches protected eval paths
        return GateDecision.BLOCK
    if s.checks is CheckResult.INFRA_ERROR:
        if s.retry_count < policy.max_retries(a.action_type):
            return GateDecision.RETRY
        return GateDecision.ESCALATE
    if s.checks is CheckResult.FAILED:
        return GateDecision.BLOCK               # dependent action only; branch may revise
    if a.action_type in policy.human_required:
        return GateDecision.REQUEST_HUMAN
    return GateDecision.AUTO_CONTINUE

def fingerprint(a: ProposedAction) -> str:
    payload = {
        "action_type": a.action_type, "target": a.target,
        "params": a.params, "code_sha": a.code_sha, "diff_sha": a.diff_sha,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(blob.encode()).hexdigest()
```

A BLOCK on failed checks stops the action, not the branch: the Coding Agent may produce a revised patch within its revision budget, which is a new action with a new fingerprint.

**Approval path**

1. Gate returns REQUEST\_HUMAN. The `human_gate` node does nothing before `interrupt()` except build the request, because LangGraph re-runs the node on resume.
2. Reviewer sees the exact action: type, target, diff, test evidence, fingerprint, reason.
3. Reviewer chooses Approve, Request revision (with notes), or Reject. The approval service stores a signed, immutable record: fingerprint, reviewer, role, decision, timestamp, expiry, single-use flag.
4. Graph resumes with `Command(resume={"approval_record_id": ...})` only.
5. `verify_approval` fetches the record and checks signature, reviewer authority, fingerprint match, expiry, and that it is unused. Revise routes to replanning; Reject routes to BLOCK.
6. On success the approval service mints a capability token scoped to that fingerprint (TTL 10 min, tune).
7. The execute node re-checks preconditions (same `code_sha`, target branch unchanged, checks still green), then calls the tool. The tool refuses without a valid token.
8. A scheduler expires stale requests after 24 h (tune) and escalates or blocks per policy.

**Initial policy**

| Action | Default outcome |
| --- | --- |
| Run backtest, analysis, research, open PR | AUTO\_CONTINUE |
| Merge to main, deploy, delete production data | REQUEST\_HUMAN |
| Modify acceptance policy, tests, harness, data splits | BLOCK for agents; humans change these only through their own reviewed PR |
| Escalate authorization, unknown action type | BLOCK |
| Infra error after retry budget, unresolved evidence conflict | ESCALATE |
| Hard gate failure on a method with a recovery rule | Recovery ladder (repair agent, then validator); see Failure recovery ladder |

Only the affected branch pauses. Research and execution branches run as separate persisted threads under a parent scheduler, so an approval wait never stalls independent work.

## Failure recovery ladder

The policy path (planner, registered methods, hard gates, `decide()`) runs without any LLM. An agent is called only when that path fails in a way a revised call might fix, and every agent proposal goes back through the same deterministic checks. `decide()` is unchanged: a failed check is still BLOCK for *that action*; the ladder is how the branch revises it.

```
 failure ─► Triage (deterministic)
              ├─ transient ──────► blind retry (ADR-011) ─► exhausted ─► Human
              ├─ forbidden / plan changed ─────────────────────────────► BLOCK / Human
              ├─ known fix (human-approved rule) ─► apply, hard gates ─► done
              └─ fixable ─► Repair agent ─► guards ─► hard gates ─┬─ pass ─► Validator ─┬─ accept ─► done
                               ▲                                   │                    └─ reject / escalate ─► Human
                               └──── reflection (budget left) ◄────┘ fail
                                                                    budget spent ─► Human
```

**Triage table** (`configs/recovery_policy.json`, protected; a change is a reviewed diff)

| Failure | Signal | Route |
| --- | --- | --- |
| Transient: timeout, API or model outage | `RetryableError` → INFRA\_ERROR | Blind retry within ADR-011 budgets, then ESCALATE to a human. No LLM: a new call does not fix an outage |
| Hard gate failed, input validation failed, method raised | FAILED | Known-fix rule if one matches; else repair agent if the method has a recovery rule; else BLOCK as today |
| Protected path, unknown action, registry or plan digest changed | BLOCK / `NeedsRevision` | BLOCK, or a human for a version change |
| Unresolved evidence conflict | ESCALATE | Human |
| Policy-listed action (merge, deploy) | REQUEST\_HUMAN | Signed approval (unchanged) |
| Repair or validator agent refuses, or fails its schema after the core's one repair | ESCALATE | Human |

**Recovery rule** (per method, in the policy file): `tunable` parameters with bounds (numeric range or allowed values), `frozen` parameters (data split, train window, sources: never changed), and `alternatives`, registered methods with the same role and output contract. A method with no rule is never repaired.

**Repair agent.** Input, built by the context layer: the task card, the failed call, the gate results and error verbatim, and every earlier attempt with its reflection. Output (schema-checked by the model core):

- `reflection`: `diagnosis`, `evidence` (quoted verbatim from the gate results or error), `hypothesis`, `changes` (the parameters or method it changes), `differs_from_previous` (why this attempt is not a repeat).
- `proposal`: `method` and `parameters`.

**Guards** (deterministic, before any execution): the method is the original or a listed alternative; every changed parameter is tunable and within bounds; frozen parameters are byte-identical; the proposal's fingerprint differs from every earlier attempt (novelty); the reflection's `evidence` occurs verbatim in the failure packet and its `changes` equal the computed diff; protected-path, split and lookahead invariants hold. A guard rejection counts as an attempt and is fed back into the next reflection. The revised call runs as a new run whose plan differs only in that call, linked to the parent run, so plan digests, approvals and gates apply unchanged.

**Budgets** (tune): 2 repair attempts per failure; a cost cap per escalation ($0.50); a circuit breaker that sends a method straight to a human once it needed the ladder in 3 of its last 10 runs (a method or policy gap, not something to keep paying for).

**Validator agent.** A separate request with a blind context: the task card (objective and criteria), the original call and its failure, the final call, the parameter diff, the final output and the gate results. It never sees the repair agent's reflections or messages. Output: `verdict` (accept, reject or escalate), `reasons`, and one finding per acceptance criterion on the card; a verdict that skips a criterion is treated as escalate. Its job is goal drift: a repair that passes the gates by changing the question. It can reject or escalate; it can never accept past a failed hard gate. A reject goes to a human even when repair budget is left, so the verdict never feeds back into the repair loop. The model is configurable (`validator_model`, default the repair agent's plug). It also audits policy-path successes at `validator_sample_rate` (tune, default 0); the sample is chosen by hashing run and task IDs, so replay stays deterministic, and a rejected sample escalates the run before knowledge ingestion.

**Human.** Escalations open a signed approval request carrying an escalation packet: the original failure, every attempt with its reflection and guard results, and the validator's verdict. The reviewer approves the final result, supplies a revised call, or rejects.

**Lessons.** An accepted repair is stored as a provisional lesson keyed by failure signature (method, gate, error class) and the change made. When 3 (tune) lessons agree, the system proposes a known-fix rule for a human to add to the policy file; known fixes then run without an LLM, through the hard gates only. Lessons never change policy by themselves (ADR-001).

## Context and knowledge layer

One deterministic `context/` package builds every LLM request from a pinned task card plus selected evidence; it merges the draft's separate Context Manager and Adaptive Context Controller.

**Request layout** (ordered for prompt caching: stable first, volatile last)

1. System prompt and tool schemas (stable per agent type).
2. Task card: goal, acceptance criteria, `code_sha`, dataset versions, constraints, budget left. Immutable for the task; injected verbatim on every call.
3. Handoff records from upstream agents (typed JSON, not transcripts).
4. Retrieved evidence, each item tagged with its evidence ID and version.
5. Recent working history (last N tool calls, tune).
6. Output reserve.

**Memory tiers**

| Tier | Holds | Trust |
| --- | --- | --- |
| Authoritative | Source at SHA, experiment artifacts, datasets, approvals | Ground truth; exact retrieval |
| Verified knowledge | Findings promoted by an independent check | Usable as evidence with its IDs |
| Provisional | Hypotheses, agent summaries | Labeled provisional; never cited as fact |
| Working | Current call's history | Disposable |

An agent cannot promote provisional knowledge to verified by writing a summary. Promotion requires a verifier pass that links the claim to authoritative artifacts.

**Selection**

- Hard filters first: permissions, version compatibility, deprecated status.
- Then rerank and fill the budget greedily with deduplication. Skip the knapsack formulation from the draft unless evals show greedy loses.
- Progressive retrieval: start small, check which required sub-questions lack evidence, retrieve only for those gaps, stop at the retrieval budget.
- Code: retrieve exact function text at the current SHA before any edit; summaries never stand in for code.

**Health triggers** (failure signals beat utilization)

| Signal | Action |
| --- | --- |
| Utilization over 70% of input budget (tune) | Drop old tool outputs, then rebuild |
| Output cites a missing file, ID, or line | Reject output, rebuild evidence |
| Output contradicts task card or a verified artifact | Rebuild from task card |
| Same tool call repeated with no state change | Stop loop, change retrieval strategy |
| Mixed run IDs or dataset versions | Reset working context |
| Two failed rebuilds in a row | ESCALATE the reasoning branch |

Rebuild means: archive the full history, construct a fresh request from the task card, handoffs, and authoritative evidence, then continue from the last verified checkpoint. Summarization middleware is allowed only for the disposable working tier.

**Knowledge freshness**

- Re-index on events: commit merge (code), new verified experiment (results), provider update (data).
- On a code change, mark current-code summaries and embeddings for that file stale; keep old versions for reproducing past runs.
- Store: Postgres with pgvector, plus AST and lexical index for code. One database to back up and audit.

## Reliability controls

These controls cover the failure modes the original design left open: duplicate side effects, runaway loops, evaluator tampering, invented numbers, and untested thresholds.

**Idempotent side effects (ADR-004)**

- Each side-effecting tool call is its own graph node, with nothing after it in that node.
- Key: `sha256(run_id + fingerprint + attempt_scope)`. Before calling, insert the key into the `side_effects` ledger; if it exists with status done, return the stored result.
- Merges, deploys, and data writes go through an outbox table processed by one worker, so a crash between "decided" and "done" never doubles the action.
- Option to evaluate: Temporal for durable execution, timers, and approval expiry, with LangGraph inside activities for reasoning. Adopt only if the custom scheduler grows complex.

**Protected evaluator (ADR-005)**

- Protected paths: `tests/`, backtest harness, acceptance criteria, data-split config, policy files. Enforced by repo branch protection and CODEOWNERS, plus the gate's invariant check.
- Hard invariants checked on every backtest: point-in-time data only, no lookahead, frozen out-of-sample window untouched by tuning.
- A change that improves in-sample but not out-of-sample is reported as not accepted, automatically.

**Number provenance (ADR-007)**

- Agents write `{{metric:artifact_id:field}}` placeholders; a renderer fills values from the experiment database.
- Post-check extracts every numeral from final outputs and fails any that is not a rendered placeholder or a quoted input.

**Budgets and circuit breakers**

| Budget | Scope | On breach |
| --- | --- | --- |
| LLM tokens and cost | Per run and per agent | ESCALATE |
| Wall-clock time | Per task | ESCALATE |
| Tool calls | Per task | Stop loop, ESCALATE |
| No-progress iterations (state hash unchanged) | Per branch | Stop branch after 3 (tune) |
| Revision attempts | Per coding task | BLOCK the action after 2 (tune) |

**Model call layer**

- Structured outputs validated against Pydantic schemas; one repair retry with the validation error, then ESCALATE.
- Model fallback list per agent for provider outages; record which model produced each output.

**Evals and observability**

- Golden tasks: a fixed suite of research, coding, and analysis tasks with known-good outcomes.
- Replay harness: re-run recorded traces with recorded tool results to test orchestration changes deterministically.
- Fault injection: API down, stale index, contradictory papers, crash mid-merge, expired approval.
- Tracing: OpenTelemetry spans per node and tool call, tagged `run_id`, `task_id`, `code_sha`, `model`.
- Every **(tune)** number in this doc is set from these evals, not intuition.

## Module layout

One tree replaces the two conflicting layouts in the draft; numerical algorithms stay untouched outside `orchestration/`.

```
orchestration/
├── graph.py               # LangGraph wiring only
├── state.py               # typed workflow state, task card
├── scheduler.py           # parent scheduler, branch threads, expiry timers
├── gate/
│   ├── decide.py          # pure decide(), fingerprint()
│   ├── human_gate.py      # interrupt node, resume handling
│   ├── approvals.py       # client for signed approval records
│   └── capabilities.py    # token minting and verification
├── policy/
│   └── *.cedar            # versioned rules (ADR-008)
├── tools/
│   ├── registry.py        # every tool declares side-effect class
│   ├── ledger.py          # idempotency ledger + outbox (ADR-004)
│   └── guarded.py         # wrapper: token check + ledger
├── context/
│   ├── task_card.py
│   ├── builder.py         # request layout, budgets
│   ├── retrieval.py       # routing, filters, rerank, progressive fill
│   ├── health.py          # triggers and rebuild
│   └── handoff.py         # typed agent handoff schemas
├── knowledge/
│   ├── ingestion.py       # event-driven re-indexing
│   ├── code_index.py      # AST + lexical, per SHA
│   ├── evidence_store.py  # tiers, promotion, provenance
│   └── freshness.py
├── verify/
│   ├── numbers.py         # placeholder rendering + numeral check
│   ├── citations.py       # file, SHA, line, artifact ID checks
│   └── invariants.py      # lookahead, splits, protected paths
├── budgets.py
├── recovery/
│   ├── triage.py          # failure taxonomy -> tier (recovery_policy.json)
│   ├── repair.py          # repair agent request/reply, reflection schema
│   ├── guards.py          # bounds, frozen params, novelty, reflection vs diff
│   ├── validator.py       # blind validator, sampling
│   ├── ladder.py          # loop, budgets, circuit breaker, escalation packet
│   └── lessons.py         # provisional lessons, known-fix proposals
├── agents/
│   ├── planner.py  researcher.py  coder.py
│   └── supervisor.py  analyst.py
└── evals/
    ├── golden/  replay/  faults/
```

## Build roadmap

Build safety against irreversible harm first, then the means to measure, then quality; each phase ends at an exit test.

1. **Safe side effects**: tool registry, idempotency ledger, outbox, capability tokens, `decide()` with unit tests.
   - Exit: fault-injection crash mid-merge causes zero duplicate merges; a privileged tool called without a token is refused.
2. **Protected evaluator**: branch protection, CODEOWNERS, invariant checks for lookahead and splits.
   - Exit: a scripted agent attempt to edit `tests/` is blocked; a leaky backtest fails the invariant check.
3. **Approval path**: human gate node, approval service, verification, expiry scheduler, branch-level pausing.
   - Exit: approve, revise, reject, and expire each route correctly; research branch keeps running during a pending merge approval.
4. **Eval and replay harness**: golden tasks, recorded traces, fault suite, tracing.
   - Exit: baseline scores recorded for every golden task.
5. **Context layer**: task card, builder, retrieval, health triggers, rebuild.
   - Exit: on long-run golden tasks, citation failures and forgotten criteria drop versus baseline; all (tune) thresholds set from data.
6. **Knowledge lifecycle and number provenance**: event re-indexing, tiers and promotion, placeholder renderer.
   - Exit: 100% of reported numbers trace to artifacts on the golden suite.
7. **Full-stack soak**: one host wiring every layer, a simulated campaign with injected faults, and a scorer over durable state.
   - Exit: on a 72 h (tune) simulated campaign with every layer on, the success-criteria table is met, measured from durable state alone; then the same campaign on a real model through the socket.
8. **Failure recovery ladder**: triage, repair agent with reflection, guards, blind validator with sampling, escalation packet, lessons and known fixes.
   - Exit: on the golden suite plus recovery faults, with simulated agents: 0 LLM calls on clean tasks and on transient faults; every fixable fault solved within 2 attempts and accepted by the validator; every unfixable fault escalated with a complete packet; 0 gate-gaming repairs accepted (caught by guards or the validator); a repeated proposal escalates on the novelty guard; a promoted known fix runs with 0 LLM calls. Measured from durable state; then the same suite with a real model through the socket.

## Open questions

- [ ] Durable execution: custom scheduler on LangGraph, or Temporal with LangGraph inside activities? (Proposed for now: the PR #13 state machine on SQLite behind interfaces, ADR-009.)
- [ ] Policy engine: Cedar or OPA? (Proposed for now: neither; versioned policy files, ADR-013.)
- [ ] Who are the authorized reviewers, and does any action need two approvers?
- [ ] Does ESCALATE go to the Supervisor Agent first, or straight to a human, for each escalation type? (Proposed: the validator agent reviews only repaired results and sampled successes; every ESCALATE goes to a human with a packet, ADR-020.)
- [x] Recovery (tune) starting values: 3 agreeing lessons propose a known fix; the circuit breaker trips at 3 ladder runs in a method's last 10; $0.50 per escalation. A validator reject goes to a human even with repair budget left, so the verdict never feeds the repair loop (settled 2026-11-23).
- [ ] Which models per agent, and what is the fallback order? (Mechanism settled by ADR-016; the choice of real models is still open.)
- [ ] Retention period for archived context histories and traces.

## Glossary

| Term | Meaning |
| --- | --- |
| Task card | Immutable record of a task's goal, criteria, versions, and constraints, injected into every call |
| Fingerprint | Hash of an action's semantic payload; approvals bind to it |
| Capability token | Short-lived signed permission for one fingerprint, checked by the tool |
| ESCALATE | Needs judgment; routed to supervisor or human |
| BLOCK | Forbidden; no override |
| Recovery ladder | Triage → repair agent with reflection → guards and hard gates → blind validator → human |
| Reflection | The repair agent's structured diagnosis of a failed attempt, checked against the failure packet and the actual diff |
| Known fix | A human-approved deterministic rule, promoted from repeated lessons, applied without an LLM |
| Rebuild | Fresh context from task card and authoritative evidence, replacing history |
| Protected evaluator | Tests, harness, criteria, and splits that agents cannot modify |

## Changelog

| Date | Version | Change |
| --- | --- | --- |
| 2026-10-30 | 0.1 | First draft consolidating the ChatGPT design thread with review fixes (ADR-001 to ADR-008) |
| 2026-11-23 | 0.5 | Folded the Phase 7 real-model decisions into ADR-021 (Claude plug defaults, recorded demos with offline replay, rebuild switches, shared report prompt, case-insensitive identifiers, tag-form citation versions, soak criterion split, transitive demotion) |
| 2026-11-23 | 0.4 | Added the failure recovery ladder section, ADR-020 and roadmap Phase 8; two success criteria (no LLM calls on the policy path, no gate-gaming repairs) |
| 2026-11-15 | 0.3 | Added roadmap Phase 7 (full-stack soak) and ADR-019 (host Stack, durable-state scorer) |
| 2026-11-15 | 0.2 | Folded the Phase 1–6 implementation decisions from `CLAUDE.md` into ADR-009 to ADR-018 (proposed); ADR-004 and ADR-008 marked superseded; proposed answers noted on open questions |
