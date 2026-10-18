# Autonomous research orchestration

The additive `finquint.autonomous` package coordinates explicit plans without
changing any existing analytics, `QuantPipeline`, Phase 10A task transitions,
Phase 10B specialist execution, or Phase 10C evaluation behavior. It shares
`ResearchStore`'s SQLite connection and append-only audit API, with a separate
`autonomous_runs` state table. Existing manually governed workflows still work.

## Design

1. **Planner:** accepts a structured `Request`, validates the dependency DAG and
   host-owned method allowlist, validates inputs, and freezes a JSON plan with
   source snapshots, parameters, code version, retries and method signatures.
2. **Research agents:** registered functions receive copies of source snapshots
   and validated dependency outputs. Their results must be exact, cited excerpts.
   No implicit browsing, shell, files, generated imports or tool execution occurs.
3. **Execution agents:** fresh deterministic adapters invoke existing functions or
   pipelines. Independent research and execution calls run in the same bounded
   thread pool; dependent calls wait for successful supervisor validation.
4. **Supervisor:** rejects empty/non-JSON/nonfinite output and unsupported research
   claims, then runs every mandatory host-owned gate on independent copies. A
   failed gate or gate exception blocks the final report. Agents cannot choose,
   omit, weaken or approve their gates.
5. **Report/analysis agent:** extracts validated outputs, source hashes, numerical
   gates, attempt counts and limitations into a JSON report. It does not generate
   uncited economic explanations or infer trading recommendations.
6. **Optional improvement agents:** a `proposal` method always needs human approval
   before invocation. It can return only patch text and rationale into the state
   store. The orchestrator never applies, imports, executes, commits or merges it.
   Applying and testing a proposal requires a separate reviewed worktree/process.

This uses a small explicit state machine rather than introducing LangGraph,
LangChain or a provider SDK. The repository already has deterministic governance
and no model dependency. Named role adapters are the multi-agent boundary; the
default planner/research/report agents are deterministic, not LLM calls. A host
can translate an LLM plan into `Request`, or register a model-backed researcher
with a bounded, read-only retrieval interface. Such output passes the same gates.
There is no natural-language-to-plan service or live model integration in this PR.
RAG is optional inside a research adapter; unrestricted ReAct/tool loops are not
part of execution. An external graph engine can call this API without replacing
the numerical implementations or weakening promotion gates.

## Run and resume

```python
from finquint.autonomous import AutonomousWorkflow, RunStore
from finquint.autonomous.adapters import default_registry
from finquint.research import ResearchStore

with ResearchStore("research.sqlite") as research:
    workflow = AutonomousWorkflow(RunStore(research), default_registry())
    workflow.submit(request)  # Request: unique run ID, sources, code version, calls
    state = workflow.run(request.run_id)
    # On restart: recreate the same host registry, omit submit, call run(run_id).
```

Run `python examples/autonomous_research.py` for a complete offline example.
The reference bond adapter uses the existing pricing and risk pipeline. Its gate
checks a YTM round trip, duration against an independent price finite difference,
and risk sign constraints. It does not certify market data or investment merit.

The registry is built by the trusted host, never supplied by a prompt. To extend
it, register `Method(name, version, role, execute, validate_input, gates,
requires_approval=False)`. `execute(parameters, context)` returns a JSON object;
`validate_input(parameters)` must reject invalid schemas; every gate accepts
`(parameters, context, output)` and returns `ValidationResult`. Always include a
method-specific numerical/domain gate; structural validation alone is inadequate.
`context` contains source snapshots, dependency outputs and code version. Dependencies
are declared in `Call`; they receive only supervisor-approved outputs.

Use `pipeline_adapter(factory, load_data=..., encode=...)` for any current/future
pipeline, or a direct function adapter for another numerical solver. Factories
must create fresh per-call pipelines and data. Explicitly encode DataFrames,
curves and domain objects to JSON and declare random seeds for stochastic methods.
Even `continue_on_error=True` pipelines are blocked if any stage failed.
`evaluation_gate` adapts a `ResearchEvaluationSuite` to a mandatory gate. Its
warning-level findings stay visible in gate details; error-level findings block.

## Risk and interrupts

The host marks risky methods `requires_approval=True`; model parameters cannot
lower this flag. Unrelated tasks continue while such calls wait. The run then
returns `awaiting_approval`. Show the stored plan, sources, parameters, code version,
method signatures and `plan_digest` to the reviewer. From an authenticated human
UI only, call:

```python
workflow.decide(run_id, task_id, plan_digest=state["plan_digest"],
                reviewer="authenticated-user-id", approved=True,
                notes="Reviewed inputs and method risk")
state = workflow.run(run_id)
```

Approval permits that exact pending call to run; it cannot override failed gates.
Multiple approvals can be handled one at a time, resuming between decisions.
Denial is terminal (`rejected`). Method/version/policy/source-plan drift requires
a new reviewed plan. Source snapshots and parameters are persisted, so modifying
the caller's objects after submission cannot change approved work.

`NeedsRevision` means a preset prompt, method or assumption does not apply. It
halts the run without retries (`needs_revision`). Review the reason and submit a
new request/run ID; do not silently rewrite the preset. Failed or rejected runs
are also terminal, preserving all prior history. Failures never produce a report.
Already-running independent read-only tasks finish and remain auditable; no new
dependency wave is scheduled after a hard failure.

## Persistence and retry semantics

Only the coordinator uses SQLite. Claims are serialized with `BEGIN IMMEDIATE`;
another coordinator cannot concurrently run the same run ID. Each wave records
attempt counts before dispatch; each validated result and its hash is committed
with an audit event. Completed tasks are not replayed on normal resume. Reports
include the plan digest and source SHA-256 values; task audit events include output
hashes, gate evidence and errors. Snapshot URIs/versions and `code_version` must
truthfully identify the inputs supplied by the host; the library cannot verify a
user-provided commit label against a remote repository.

Only adapter-raised `RetryableError` requests a retry, capped at 1–5 total attempts
per task (default 2). Ordinary exceptions, permission/schema failures, hard gate
failures and prompt mismatches do not retry. No unbounded repair/replanning loop
exists. Retries reuse identical frozen inputs. Successful peers are not repeated.

A crash leaves `running` state rather than assuming success. Stop/verify the old
worker process first; an authenticated operator can then call
`recover(run_id, reviewer=..., notes=...)`. Recovery preserves consumed attempts
and validated outputs. Interrupted pure calls may replay within the original
budget; this is at-least-once execution, not exactly-once external effects.

## Trust boundaries and constraints

- Python adapters and gates are trusted application code. Copies and narrow
  arguments prevent accidental shared-state changes; threads are **not** an OS
  security sandbox. Never load arbitrary Python, generated code or untrusted
  plugins into this process. For model/tool runners use a separate process or
  container with filesystem/network restrictions and an authenticated host API.
- Proposal isolation here is inert text storage, not a code execution sandbox.
  There are no automatic code edits, live trading, external mutations or merges.
- Human identities are recorded, not authenticated by this library. Do not expose
  `decide`, `recover`, registry mutation or SQLite access to agents. Recovery must
  not race a live process; this version has no distributed leases or heartbeat.
- Threads are suitable for bounded trusted calls and I/O overlap, not CPU scaling
  or enforceable timeouts. Providers must implement request deadlines/cancellation
  and resource budgets. A hung Python adapter cannot be forcibly killed here.
- Source text is data, never instructions. Exact quotations prevent unsupported
  generated claims in reports; they do not establish source truth, economic
  causality, out-of-sample validity, or absence of leakage. Register domain checks
  and review source quality explicitly for each new method.
- Method signatures include function identities/source hashes where inspectable.
  Host method versions must also change for closure values, dependencies, seeds,
  model configuration or policy changes; these cannot all be inferred from code.
- SQLite and audit events are local provenance, not tamper-proof storage against
  a privileged database writer. Protect and back up the database at the host layer.

## Repository inspection and validation scope

The integration follows the full package layout: data providers/schema,
pipeline/context, fixed-income pricing/settlement/curves, numerical helpers,
short-rate models, historical/portfolio risk, backtesting, performance,
research governance/store/artifacts, specialist agents, evaluation, examples and
CI. Existing analytics remain untouched. The adapter seam avoids a registry of
hard-coded fixed-income-only methods: hosts can wrap all current pipelines and
future solvers with their own schemas, encoders and domain gates.

Regression tests exercise parallel overlap, dependency ordering, invalid source
claims, hard gates, nonfinite output, retries, human approval/denial, restart,
registry drift, interrupted-run recovery, proposal storage and the existing
evaluation/pipeline adapters. CI retains its Python 3.10–3.12 matrix.
