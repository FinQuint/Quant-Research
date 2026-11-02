> Repo copy of the Claude Doc spec (v0.1, 2026-10-30). Live doc: https://claude.ai/artifact/A8HdytgAaEqZrKU8o1dijF
> Implementation decisions that refine this spec are in `CLAUDE.md` ("Decisions") until folded back into an ADR.

# FinQuint Reliable Agentic Workflow — Architecture Spec

Oct 30, 2026 · @Yunan Xie

## Document control

This is the single source of truth for the FinQuint orchestration architecture; version 0.1, draft for review.

| Field | Value |
| --- | --- |
| Version | 0.1 (draft) |
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

## Architecture overview

Agents propose; deterministic code verifies, gates, and executes.

_(Control-path diagram: see the live doc.)_

Agents never touch side effects directly. Outputs are verified, then gated; only guarded tools act, and they check a capability token and the idempotency ledger. RETRY goes back through the scheduler, and results land as artifacts the context builder reads next.

## Architecture decision records

Eight decisions define the system; all are Proposed until reviewed. Supersede an ADR by adding a new row and setting the old one to Superseded.

| ADR | Decision | Why | Status |
| --- | --- | --- | --- |
| ADR-001 | Policy, gating, and context budgeting are deterministic code, not LLM agents | Auditable, testable, cannot be talked out of a rule | Proposed |
| ADR-002 | Gate has five outcomes: AUTO\_CONTINUE, RETRY, REQUEST\_HUMAN, ESCALATE, BLOCK | Separates "needs judgment" (ESCALATE) from "forbidden" (BLOCK) | Proposed |
| ADR-003 | Enforcement lives in the tool layer via short-lived signed capability tokens; graph gate is advisory routing | No graph path, prompt injection, or tool substitution can bypass approval | Proposed |
| ADR-004 | Every side-effecting call is its own node with an idempotency key in a Postgres ledger; outbox for merges, deploys, writes | LangGraph replays a node on resume; duplicates must be impossible | Proposed |
| ADR-005 | Tests, backtest harness, acceptance criteria, and data splits are read-only to agents | Prevents the coding agent from "passing" by changing the evaluator | Proposed |
| ADR-006 | Context is rebuilt from a pinned task card plus authoritative artifacts, not chained summaries | Summaries of summaries lose and distort facts | Proposed |
| ADR-007 | LLMs never emit metrics; outputs cite artifact IDs and a renderer inserts numbers | Eliminates hallucinated results | Proposed |
| ADR-008 | Policy rules live in a policy engine (Cedar or OPA), versioned, changes require approval | Policy changes become reviewable diffs | Proposed |

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

Only the affected branch pauses. Research and execution branches run as separate persisted threads under a parent scheduler, so an approval wait never stalls independent work.

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

## Open questions

- [ ] Durable execution: custom scheduler on LangGraph, or Temporal with LangGraph inside activities?
- [ ] Policy engine: Cedar or OPA?
- [ ] Who are the authorized reviewers, and does any action need two approvers?
- [ ] Does ESCALATE go to the Supervisor Agent first, or straight to a human, for each escalation type?
- [ ] Which models per agent, and what is the fallback order?
- [ ] Retention period for archived context histories and traces.

## Glossary

| Term | Meaning |
| --- | --- |
| Task card | Immutable record of a task's goal, criteria, versions, and constraints, injected into every call |
| Fingerprint | Hash of an action's semantic payload; approvals bind to it |
| Capability token | Short-lived signed permission for one fingerprint, checked by the tool |
| ESCALATE | Needs judgment; routed to supervisor or human |
| BLOCK | Forbidden; no override |
| Rebuild | Fresh context from task card and authoritative evidence, replacing history |
| Protected evaluator | Tests, harness, criteria, and splits that agents cannot modify |

## Changelog

| Date | Version | Change |
| --- | --- | --- |
| 2026-10-30 | 0.1 | First draft consolidating the ChatGPT design thread with review fixes (ADR-001 to ADR-008) |
