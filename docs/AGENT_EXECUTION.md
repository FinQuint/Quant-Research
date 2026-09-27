# Controlled specialist-agent execution

Phase 10B adds local specialist agents on top of Phase 10A governance. Agents do
not receive raw operating-system or GitHub access. They operate through a narrow
`AgentExecutionContext` whose capabilities are declared in an immutable manifest.

Supported capabilities are workspace reads, workspace writes, approved
validations, and artifact registration. Every action consumes a step and appears
in a structured execution trace. Budgets limit steps, changed files, and
artifacts. Relative paths are resolved against an isolated task directory;
absolute paths and traversal outside that directory are rejected.

Validations are named Python callables registered by the host application, not
commands supplied by an agent. An agent cannot invoke an unregistered validation.
Execution errors and permission violations become structured failed results and
task state, preserving evidence for revision.

`AgentExecutor` runs every currently ready task. A task waiting for human
approval does not block unrelated ready work or dependency chains. Results still
enter Phase 10A independent review and approval gates; execution does not imply
review or approval.

## Trust boundaries

This phase intentionally excludes arbitrary shell commands, network access,
dynamic package installation, repository pushes, pull-request merges, production
credentials, and governance self-modification. A future integration may expose
additional tools only as separately reviewed capabilities with explicit input
schemas, resource limits, audit events, and human gates.
