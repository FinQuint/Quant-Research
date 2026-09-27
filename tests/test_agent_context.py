from pathlib import Path

import pytest

from finquint.agents import (
    AgentCapability, AgentExecutionContext, AgentManifest, ExecutionBudget,
    ValidationResult,
)
from finquint.research import ResearchTask


def _task():
    return ResearchTask("task", "Create a research artifact", ("Validation passes",), assigned_agent="writer")


def test_capability_scoped_read_write_and_trace(tmp_path):
    manifest = AgentManifest("writer", "implementation", frozenset({
        AgentCapability.READ_WORKSPACE, AgentCapability.WRITE_WORKSPACE,
    }))
    context = AgentExecutionContext(manifest, _task(), tmp_path / "sandbox")
    context.write_text("reports/result.txt", "reproducible")
    assert context.read_text("reports/result.txt") == "reproducible"
    assert context.result(success=True).changed_files == ("reports/result.txt",)
    assert [entry.action for entry in context.trace] == ["write", "read"]


def test_context_rejects_path_escape_and_absolute_paths(tmp_path):
    manifest = AgentManifest("writer", "implementation", frozenset({AgentCapability.WRITE_WORKSPACE}))
    context = AgentExecutionContext(manifest, _task(), tmp_path / "sandbox")
    with pytest.raises(PermissionError, match="escapes"):
        context.write_text("../outside.txt", "no")
    with pytest.raises(PermissionError, match="absolute"):
        context.write_text(str((tmp_path / "absolute.txt").resolve()), "no")


def test_context_enforces_capabilities_and_file_budget(tmp_path):
    readonly = AgentExecutionContext(
        AgentManifest("reader", "review", frozenset({AgentCapability.READ_WORKSPACE})),
        _task(), tmp_path / "read",
    )
    with pytest.raises(PermissionError, match="write_workspace"):
        readonly.write_text("x", "x")
    limited = AgentExecutionContext(
        AgentManifest("writer", "implementation", frozenset({AgentCapability.WRITE_WORKSPACE}),
                      ExecutionBudget(max_changed_files=1)),
        _task(), tmp_path / "limited",
    )
    limited.write_text("one.txt", "1")
    with pytest.raises(RuntimeError, match="file budget"):
        limited.write_text("two.txt", "2")


def test_only_registered_validations_can_run(tmp_path):
    def schema_check(context, task):
        return ValidationResult("schema", True, "schema is valid", {"rows": 10})

    manifest = AgentManifest("writer", "implementation", frozenset({AgentCapability.RUN_VALIDATIONS}))
    context = AgentExecutionContext(manifest, _task(), tmp_path, validations={"schema": schema_check})
    assert context.run_validation("schema").passed
    assert context.result(success=True).metrics["schema.rows"] == 10
    with pytest.raises(PermissionError, match="not approved"):
        context.run_validation("shell-command")


def test_step_budget_is_enforced(tmp_path):
    context = AgentExecutionContext(
        AgentManifest("writer", "implementation", frozenset({AgentCapability.WRITE_WORKSPACE}),
                      ExecutionBudget(max_steps=1)),
        _task(), tmp_path,
    )
    context.write_text("one.txt", "1")
    with pytest.raises(RuntimeError, match="step budget"):
        context.write_text("one.txt", "2")
