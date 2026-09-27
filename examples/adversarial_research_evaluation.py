"""Run the Phase 10C adversarial research gate on a synthetic study."""
import pandas as pd

from finquint.evaluation import (
    BenchmarkCheck,
    DataQualityCheck,
    MultipleTestingCheck,
    OutOfSampleCheck,
    PointInTimeCheck,
    QuantResearchEvaluationStage,
    ResearchEvaluationSuite,
    SubperiodStabilityCheck,
    TransactionCostCheck,
)
from finquint.pipeline import QuantPipeline


events = pd.date_range("2025-01-01", periods=40, freq="D", tz="UTC")
study = pd.DataFrame({
    "observation_id": range(40),
    "event_time": events,
    "availability_time": events + pd.Timedelta(hours=1),
    "decision_time": events + pd.Timedelta(hours=2),
    "sample": ["train"] * 20 + ["test"] * 20,
    "strategy_return": [0.001, -0.0005, 0.0008, 0.0002] * 10,
})

suite = ResearchEvaluationSuite((
    PointInTimeCheck(),
    DataQualityCheck(key_columns=("observation_id",)),
    OutOfSampleCheck(minimum_test_observations=20),
    BenchmarkCheck(),
    TransactionCostCheck(),
    MultipleTestingCheck(),
    SubperiodStabilityCheck(maximum_dispersion=0.5),
))

metadata = {
    "benchmark": "cash_plus_200bps",
    "annual_turnover": 1.5,
    "transaction_cost_bps": 8.0,
    "tested_variants": 4,
    "multiple_testing_correction": "holm",
    "subperiod_metrics": (0.72, 0.81, 0.69),
}

result = QuantPipeline("adversarial_research_gate").add(
    QuantResearchEvaluationStage(suite, metadata=metadata, fail_on_error=True)
).run(study)

report = result.context.get("research_evaluation")
print("Research accepted:", report["passed"])
print("Checks evaluated:", len(report["results"]))
print("Warnings:", report["warnings"])
