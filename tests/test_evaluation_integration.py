import pandas as pd
import pytest

from finquint.agents import AgentCapability, AgentExecutionContext, AgentManifest
from finquint.evaluation import (
    BenchmarkCheck,
    QuantResearchEvaluationStage,
    ResearchEvaluationSuite,
    build_agent_validation,
)
from finquint.pipeline import QuantPipeline
from finquint.research import ResearchTask


def test_pipeline_stage_publishes_structured_report():
    stage = QuantResearchEvaluationStage(
        ResearchEvaluationSuite((BenchmarkCheck(),)), metadata={"benchmark": "Treasury index"}
    )
    result = QuantPipeline("research_gate").add(stage).run(pd.DataFrame({"return": [0.01]}))
    assert result.context.get("research_evaluation")["passed"] is True
    assert result.context.metrics[stage.name]["status"] == "success"


def test_pipeline_stage_can_block_failed_research():
    stage = QuantResearchEvaluationStage(
        ResearchEvaluationSuite((BenchmarkCheck(),)), fail_on_error=True
    )
    with pytest.raises(ValueError, match="benchmark_declared"):
        QuantPipeline().add(stage).run(pd.DataFrame({"return": [0.01]}))


def test_evaluation_can_run_as_approved_agent_validation(tmp_path):
    suite = ResearchEvaluationSuite((BenchmarkCheck(),))
    validation = build_agent_validation(
        "adversarial_research", suite, pd.DataFrame({"return": [0.01]}),
        metadata={"benchmark": "Treasury index"},
    )
    task = ResearchTask("evaluate", "Evaluate the study", ("Research gate passes",))
    manifest = AgentManifest(
        "quant-reviewer", "independent research reviewer",
        frozenset({AgentCapability.RUN_VALIDATIONS}),
    )
    context = AgentExecutionContext(
        manifest, task, tmp_path, validations={"adversarial_research": validation}
    )
    result = context.run_validation("adversarial_research")
    assert result.passed
    assert result.metrics == {"checks": 1, "failures": 0, "warnings": 0}
    assert context.trace[0].outcome == "passed"
