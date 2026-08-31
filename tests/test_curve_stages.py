import pandas as pd
import pytest

from finquint.data import QuantDataset
from finquint.data.connectors import CSVProvider
from finquint.data.stages import LoadDataStage, NormalizeDataStage, ValidateDataStage
from finquint.data.validation import Schema
from finquint.fixed_income import Bond, BondQuote
from finquint.fixed_income.stages import BootstrapYieldCurveStage, CurveBondPricingStage
from finquint.pipeline import PipelineContext, QuantPipeline


def frame():
    return pd.DataFrame({
        "face_value": [100, 100], "coupon_rate": [0, 0.04], "maturity_years": [0.5, 1],
        "frequency": [2, 2], "market_price": [98, 99],
    })


def test_csv_to_curve_to_price_pipeline(tmp_path):
    path = tmp_path / "quotes.csv"
    frame().to_csv(path, index=False)
    result = (QuantPipeline("curve_research")
        .add(LoadDataStage(CSVProvider(path)))
        .add(NormalizeDataStage())
        .add(ValidateDataStage(Schema(tuple(frame().columns))))
        .add(BootstrapYieldCurveStage())
        .add(CurveBondPricingStage(Bond(100, 0.04, 1)))
        .run())
    assert result.context.results["curve_bond_price"] == pytest.approx(99)
    assert len(result.data.data) == 2
    assert max(abs(q["price_error"]) for q in result.context.get("curve_calibration")) < 1e-8
    assert all(m["status"] == "success" for m in result.context.metrics.values())
    assert "ytm" not in result.context.results


def test_explicit_quotes_and_explicit_curve_preserve_data():
    quote = BondQuote(Bond(100, 0, 1), 96)
    stage = BootstrapYieldCurveStage([quote])
    context = PipelineContext()
    marker = object()
    assert stage.run(marker, context) is marker
    assert CurveBondPricingStage(quote.bond, curve=context.get("yield_curve")).run(marker, context) is marker
    assert context.get("curve_bond_price") == pytest.approx(96)


@pytest.mark.parametrize("column,value", [("frequency", 1.5), ("frequency", 0), ("market_price", None), ("coupon_rate", "bad")])
def test_bad_dataset_reports_row(column, value):
    data = frame().astype(object)
    data.loc[0, column] = value
    with pytest.raises(ValueError, match="row 0"):
        BootstrapYieldCurveStage().run(QuantDataset(data), PipelineContext())


def test_missing_inputs_and_pipeline_error_capture():
    context = PipelineContext()
    with pytest.raises(ValueError, match="YieldCurve"):
        QuantPipeline().add(CurveBondPricingStage(Bond(100, 0, 1))).run(context=context)
    assert context.errors[0]["stage"] == "curve_bond_pricing"
    with pytest.raises(TypeError):
        BootstrapYieldCurveStage().run(None, PipelineContext())
    for data in [frame().drop(columns="market_price"), frame().iloc[:0]]:
        with pytest.raises(ValueError):
            BootstrapYieldCurveStage().run(QuantDataset(data), PipelineContext())

