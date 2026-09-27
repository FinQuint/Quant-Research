from finquint.agents import ValidationResult
from finquint.pipeline import PipelineContext, PipelineStage

from .core import ResearchEvaluationSuite


class QuantResearchEvaluationStage(PipelineStage):
    name = "quant_research_evaluation"

    def __init__(self, suite: ResearchEvaluationSuite, *, metadata=None, fail_on_error=False):
        self.suite, self.metadata, self.fail_on_error = suite, dict(metadata or {}), fail_on_error

    def run(self, data, context: PipelineContext):
        report = self.suite.evaluate(data, metadata=self.metadata)
        context.set("research_evaluation", report.to_dict())
        if self.fail_on_error and not report.passed:
            raise ValueError(f"research evaluation failed: {', '.join(report.failures)}")
        return data


def build_agent_validation(name: str, suite: ResearchEvaluationSuite, data, *, metadata=None):
    """Adapt a frozen evaluation input into a Phase 10B approved validation."""
    if not name.strip():
        raise ValueError("validation name is required")

    def validation(context, task):
        report = suite.evaluate(data, metadata=metadata)
        details = "all error-level checks passed" if report.passed else f"failures: {', '.join(report.failures)}"
        metrics = {"checks": len(report.results), "failures": len(report.failures), "warnings": len(report.warnings)}
        return ValidationResult(name, report.passed, details, metrics)
    return validation
