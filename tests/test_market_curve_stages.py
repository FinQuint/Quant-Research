import pandas as pd
import pytest

from finquint.data import QuantDataset
from finquint.fixed_income import MultiCurveSet
from finquint.fixed_income.stages import BootstrapMarketCurveStage, BuildMultiCurveStage
from finquint.pipeline import QuantPipeline


def _quotes():
    return QuantDataset(pd.DataFrame([
        {"curve_name": "discount", "instrument_type": "deposit", "maturity": 0.5, "rate": 0.03, "accrual": 0.5},
        {"curve_name": "discount", "instrument_type": "swap", "maturity": 1.0, "rate": 0.035, "frequency": 2},
        {"curve_name": "projection", "instrument_type": "deposit", "maturity": 0.5, "rate": 0.04, "accrual": 0.5},
        {"curve_name": "projection", "instrument_type": "swap", "maturity": 1.0, "rate": 0.045, "frequency": 2},
    ]))


def test_dataset_to_multi_curve_pipeline():
    result = (QuantPipeline("multi_curve")
              .add(BootstrapMarketCurveStage(curve_name="discount", result_key="discount_curve"))
              .add(BootstrapMarketCurveStage(curve_name="projection", result_key="projection_curve"))
              .add(BuildMultiCurveStage()).run(_quotes()))
    assert isinstance(result.context.results["multi_curve"], MultiCurveSet)
    assert result.context.results["projection_curve"].zero_rate(1) > result.context.results["discount_curve"].zero_rate(1)


def test_stage_reports_bad_schema_and_missing_curves():
    with pytest.raises(ValueError, match="missing market quote"):
        QuantPipeline().add(BootstrapMarketCurveStage()).run(QuantDataset(pd.DataFrame({"rate": [0.03]})))
    with pytest.raises(Exception, match="discount and projection"):
        QuantPipeline().add(BuildMultiCurveStage()).run()


def test_stage_rejects_unknown_instrument():
    data = QuantDataset(pd.DataFrame([{"instrument_type": "future", "maturity": 1, "rate": 0.03}]))
    with pytest.raises(ValueError, match="unsupported"):
        QuantPipeline().add(BootstrapMarketCurveStage()).run(data)
