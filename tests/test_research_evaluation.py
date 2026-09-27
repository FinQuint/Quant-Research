import pandas as pd
import pytest

from finquint.evaluation import (
    BenchmarkCheck,
    DataQualityCheck,
    MultipleTestingCheck,
    OutOfSampleCheck,
    PointInTimeCheck,
    ResearchEvaluationSuite,
    SubperiodStabilityCheck,
    TransactionCostCheck,
)


def research_frame():
    event = pd.date_range("2025-01-01", periods=40, freq="D", tz="UTC")
    return pd.DataFrame({
        "observation_id": range(40),
        "event_time": event,
        "availability_time": event + pd.Timedelta(hours=1),
        "decision_time": event + pd.Timedelta(hours=2),
        "sample": ["train"] * 20 + ["test"] * 20,
        "return": [index / 1000 for index in range(40)],
    })


def research_metadata():
    return {
        "benchmark": "cash_plus_200bps",
        "annual_turnover": 1.5,
        "transaction_cost_bps": 8.0,
        "tested_variants": 4,
        "multiple_testing_correction": "holm",
        "subperiod_metrics": (0.75, 0.82, 0.71),
    }


def complete_suite():
    return ResearchEvaluationSuite((
        PointInTimeCheck(),
        DataQualityCheck(key_columns=("observation_id",)),
        OutOfSampleCheck(),
        BenchmarkCheck(),
        TransactionCostCheck(),
        MultipleTestingCheck(),
        SubperiodStabilityCheck(),
    ))


def test_complete_research_design_passes():
    report = complete_suite().evaluate(research_frame(), metadata=research_metadata())
    assert report.passed
    assert report.failures == ()
    assert report.warnings == ()
    assert report.to_dict()["results"][0]["severity"] == "error"


def test_point_in_time_lookahead_is_rejected():
    frame = research_frame()
    frame.loc[0, "availability_time"] = frame.loc[0, "decision_time"] + pd.Timedelta(seconds=1)
    report = ResearchEvaluationSuite((PointInTimeCheck(),)).evaluate(frame)
    assert not report.passed
    assert report.failures == ("point_in_time_integrity",)
    assert report.results[0].metrics["violations"] == 1


def test_data_quality_rejects_duplicates_and_missing_values():
    frame = research_frame()
    frame.loc[1, "observation_id"] = 0
    frame.loc[2, "return"] = None
    report = ResearchEvaluationSuite((DataQualityCheck(key_columns=("observation_id",)),)).evaluate(frame)
    assert not report.passed
    assert report.results[0].metrics["duplicate_rows"] == 1
    assert report.results[0].metrics["missing_fraction"] > 0


def test_out_of_sample_requires_sufficient_frozen_test_data():
    frame = research_frame()
    frame["sample"] = ["train"] * 39 + ["test"]
    report = ResearchEvaluationSuite((OutOfSampleCheck(minimum_test_observations=20),)).evaluate(frame)
    assert not report.passed


@pytest.mark.parametrize("metadata", [
    {},
    {"annual_turnover": 2.0, "transaction_cost_bps": 0.0},
])
def test_transaction_costs_cannot_be_silently_omitted(metadata):
    report = ResearchEvaluationSuite((TransactionCostCheck(),)).evaluate(research_frame(), metadata=metadata)
    assert not report.passed


def test_multiple_testing_requires_approved_correction():
    suite = ResearchEvaluationSuite((MultipleTestingCheck(),))
    assert not suite.evaluate(research_frame(), metadata={"tested_variants": 9}).passed
    assert suite.evaluate(research_frame(), metadata={
        "tested_variants": 9, "multiple_testing_correction": "fdr"
    }).passed


def test_unstable_subperiods_warn_without_overriding_error_gate():
    report = ResearchEvaluationSuite((SubperiodStabilityCheck(maximum_dispersion=0.1),)).evaluate(
        research_frame(), metadata={"subperiod_metrics": (1.0, -1.0, 3.0)}
    )
    assert report.passed
    assert report.warnings == ("subperiod_stability",)


def test_suite_rejects_duplicate_check_identifiers():
    with pytest.raises(ValueError, match="unique"):
        ResearchEvaluationSuite((BenchmarkCheck(), BenchmarkCheck()))
