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
