# Eval and replay harness (Phase 4)

`finquint.orchestration.evals` scores the orchestration stack on a fixed suite of golden
tasks and faults, replays recorded traces, and compares every run with a recorded baseline.
Spec: `docs/AGENTIC_WORKFLOW_SPEC.md`, "Evals and observability".

## Commands (repo root, `$env:PYTHONPATH = "src"`)

```powershell
python -m finquint.orchestration.evals              # live run, compared with the baseline
python -m finquint.orchestration.evals --replay     # replay every recorded trace
python -m finquint.orchestration.evals --task bond_risk_analysis --json
```

Exit status is 1 on any regression or replay divergence. `pytest` runs the same checks
(`tests/test_orchestration_evals.py`).

## What is in the suite

| Kind | Tasks |
| --- | --- |
| research | `research_cited_findings` |
| analysis | `bond_risk_analysis`, `backtest_lookahead_invariant` |
| coding | `coding_proposal_signed_approval`, `coding_protected_paths_blocked`, `merge_with_parallel_research` |
| fault | API down (transient, persistent), stale index, contradictory sources, crash mid-merge (with and without reconcile), expired approval |

Each task runs in a fresh throwaway SQLite database through the real planner, supervisor
gates, `decide()`, approvals, guarded tools, ledger and scheduler. It returns JSON facts,
and deterministic criteria grade them; nothing is graded by a model.

Known gap in the baseline: `fault_contradictory_sources` scores 0.333. No evidence-conflict
check exists yet, so the run completes instead of escalating.

## Scores

`score` is criteria passed / criteria total. These metrics must not rise: `criteria_missed`,
`citation_failures`, `safety_failures`, `duplicate_side_effects`, `untagged_spans`. These
are tracked and reported when they change: `human_interrupts`, `tool_calls`, `spans`.

## Replay

Tool handlers, reconcile checks and method executions are recorded. On replay they are served
from the trace and never called; gates, invariants, the ledger, approvals and the scheduler run
for real. A replay reports a divergence when the orchestration asks for a call the trace does
not have, skips a recorded call, or reaches different facts or audit events.

## Changing the baseline (humans only)

The package, its baseline (`evals/baselines.json`) and traces (`evals/replay/traces/`) are
protected paths with a CODEOWNERS entry. After an intended change, find out why scores or
traces moved, then re-record in a reviewed PR:

```powershell
python -m finquint.orchestration.evals --write-baseline --record-traces
```

## Tracing

`finquint.orchestration.Tracer` records spans for `workflow.task`, `scheduler.step`,
`gate.decide`, `tool.call`, `scheduler.resume`, `scheduler.expire`, `outbox.dispatch` and
`outbox.recover`. Each is tagged `run_id`, `task_id`, `code_sha`, `model`. Pass `tracer=` to
`AutonomousWorkflow`, `Scheduler`, `GuardedTools` and `OutboxWorker`; tracing is off by
default. `SQLiteSpanStore` saves spans and `to_otlp_json()` exports them for an
OpenTelemetry collector without adding a dependency.

## Long-run context evals (Phase 5)

`finquint.orchestration.evals.context` is the Phase 5 exit test: "on long-run golden tasks,
citation failures and forgotten criteria drop versus baseline; all (tune) thresholds set from
data."

```powershell
python -m finquint.orchestration.evals.context            # compare with the baseline and config
python -m finquint.orchestration.evals.context --sweep    # also re-run the threshold sweeps
python -m finquint.orchestration.evals.context --write-baseline   # humans only
```

**Tasks.** Four scripted runs of 30–63 steps with 6–7 acceptance criteria each. Every
transcript overflows the input budget. They contain the events that break long contexts:
evidence updated out of band, another run's output, a result computed on an old dataset
version, notes that cite old versions, and results that exist only in recent history.
`longrun_conflicting_sources` has two sources that disagree; the correct outcome there is to
escalate.

**Strategies.** Both use the same `SimulatedModel` (2,048-token window, 1,400-token
attention span).

- `naive`: the transcript, with the oldest messages folded into a running summary (the
  "chained summaries" ADR-006 rejects). The summary is 15% of the budget.
- `builder`: the context layer (pinned task card, retrieval, budgets, health triggers,
  rebuild).

**Scores.** Each run is scored on:

- Forgotten criteria: on the card but not in the report.
- Citation failures: an addressed criterion with no citation, or a citation that is unknown,
  superseded, deprecated or from another run.
- Wrong status: completed when it should have escalated, or the reverse.
- Model calls, rebuilds and input tokens.

| Total (4 tasks) | forgotten | citation failures | wrong status | model calls | input tokens |
| --- | --- | --- | --- | --- | --- |
| naive | 16 | 1 | 1 | 4 | 6,914 |
| builder | 0 | 0 | 0 | 3 | 3,937 |

The naive result depends on its summary size, so `naive_sensitivity` in the baseline records it
at 10%, 15% and 25%. A bigger summary forgets fewer criteria but makes more citation errors
(25%: 0 forgotten, 7 citation failures). The builder beats every row.

**Sweeps.** The (tune) values in `configs/context_policy.json` come from these tasks:

- `utilization_threshold` × `history_last_n`, grid 0.5–0.9 × 2–16. Picked: **0.8** and **16**.
  - Above 0.8, requests outgrow the simulated attention span, so evidence in the middle is
    missed and extra calls are needed.
  - Below 0.8, rebuilds trim needed evidence.
  - Fewer than 16 entries drops results that exist only in history.
- `max_failed_rebuilds`, against a model that recovers after 1 or 2 bad outputs and one that
  never recovers. Picked: **2**.
  - 1 escalates a run that would have recovered.
  - 3 wastes a call before escalating.

These picks hold for the simulated plug. Its attention span is an assumption, so re-run the
sweep for each real model plug; baselines are kept per model.

**Limits.**

- The simulated model measures the context mechanism, not real model behavior.
- `fault_contradictory_sources` in the Phase 4 suite still shows its known gap. That task
  calls the PR #13 workflow directly, which does not use the context layer yet. The context
  layer's conflict check is covered by `longrun_conflicting_sources`.

The baseline (`evals/context/baseline.json`) and `configs/context_policy.json` are protected
paths. `tests/test_orchestration_context.py` fails if either one drifts from a fresh sweep.
