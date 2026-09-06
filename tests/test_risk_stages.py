from datetime import date

import pandas as pd
import pytest

from finquint.data import QuantDataset
from finquint.fixed_income import DiscountCurve
from finquint.pipeline import PipelineContext, QuantPipeline
from finquint.risk import Portfolio, Position, CurveScenario
from finquint.risk.stages import CurveRiskStage, PortfolioRiskStage
from finquint.fixed_income import DatedBond


def inputs():
    curve = DiscountCurve.from_zero_rates((2, 5), (.03, .04))
    bond = DatedBond(100, .03, date(2024, 1, 1), date(2026, 1, 1), 1)
    return curve, Portfolio((Position("A", bond, 10),))


def test_curve_and_portfolio_risk_stages_preserve_data():
    curve, portfolio = inputs()
    scenarios = (CurveScenario("up", ((2, 10), (5, 10))),)
    marker = object()
    result = (QuantPipeline().add(CurveRiskStage(scenarios, curve=curve))
              .add(PortfolioRiskStage(date(2024, 1, 1), portfolio=portfolio, curve=curve,
                                      key_maturities=(2, 5), scenarios=scenarios)).run(marker))
    assert result.data is marker
    assert set(result.context.get("curve_scenarios")) == {"up"}
    assert result.context.get("portfolio_risk")["scenarios"][0]["pnl"] < 0


def test_portfolio_stage_loads_dataset_and_curve_from_context():
    curve, _ = inputs()
    dataset = QuantDataset(pd.DataFrame({
        "identifier": ["A"], "quantity": [10], "face_value": [100], "coupon_rate": [.03],
        "issue_date": ["2024-01-01"], "maturity_date": ["2026-01-01"],
        "frequency": [1], "day_count": ["ACT/ACT ISDA"],
    }))
    context = PipelineContext(results={"yield_curve": curve})
    result = QuantPipeline().add(PortfolioRiskStage(date(2024, 1, 1), key_maturities=(2, 5))).run(dataset, context)
    assert result.context.get("portfolio_risk")["base_market_value"] > 0


def test_stage_missing_inputs_and_duplicate_scenarios():
    curve, portfolio = inputs()
    scenario = CurveScenario("same", ((2, 1),))
    with pytest.raises(ValueError):
        CurveRiskStage(())
    with pytest.raises(ValueError):
        CurveRiskStage((scenario, scenario))
    with pytest.raises(ValueError):
        CurveRiskStage((scenario,)).run(None, PipelineContext())
    with pytest.raises(TypeError):
        PortfolioRiskStage(date(2024, 1, 1), curve=curve).run(None, PipelineContext())
    with pytest.raises(ValueError):
        PortfolioRiskStage(date(2024, 1, 1), portfolio=portfolio).run(None, PipelineContext())
