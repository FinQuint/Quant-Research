# Governed quantitative research workflows

Phase 10A establishes the auditable foundation for future research agents. It
does not grant autonomous agents permission to publish, merge, change governance,
or operate on production data.

## Workflow

Research begins with a hypothesis and a dependency-checked task plan. Tasks move
through `planned`, `ready`, `running`, `reviewing`, `awaiting_approval`, and
`completed`, with explicit `failed` and `rejected` paths. A rejected or failed
task may be revised, but its earlier audit events and results are retained.

Implementation agents cannot review their own assigned tasks. Passing review
does not bypass a required human gate. Approval records contain the reviewer,
decision notes, and optional approved commit. Retrospective changes are proposals
and must require human approval.

## Reproducibility and lineage

Experiment records bind a hypothesis to a code commit, named dataset versions,
parameters, random seed, environment metadata, metrics, and status. Artifacts are
streamed through SHA-256 and registered with their size and media type. SQLite
enforces artifact-to-experiment relationships. Audit events are append-only
through the public API and ordered by a monotonic sequence.

SQLite stores metadata only. Large datasets and results should remain in
versioned files or object storage, referenced by immutable hashes. Secrets must
never be recorded in parameters, environment metadata, results, or audit events.

## Trust boundaries

This phase deliberately excludes arbitrary shell execution, remote LLM calls,
automatic Git pushes or merges, production credentials, policy self-modification,
and claims that agent review replaces qualified human model-risk review. The
next phase may add sandboxed specialist agents through the structured
`AgentResult` contract without weakening these boundaries.

For real studies, plans should explicitly test availability timestamps,
look-ahead and survivorship bias, leakage, transaction costs, out-of-sample
behavior, parameter stability, turnover, liquidity, and benchmark selection.
