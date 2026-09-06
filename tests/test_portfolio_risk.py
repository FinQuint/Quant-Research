from datetime import date

import pandas as pd
import pytest

from finquint.data import QuantDataset
from finquint.fixed_income import DatedBond, DayCount, DiscountCurve, dirty_price_with_curve
from finquint.risk import (
    CurveScenario, Portfolio, Position, load_portfolio, portfolio_risk_report,
    steepener_scenario, twist_scenario,
)


SETTLEMENT = date(2024, 1, 1)


def portfolio():
    return Portfolio((
        Position("2Y", DatedBond(100, .03, date(2024, 1, 1), date(2026, 1, 1), 1), 10),
        Position("5Y", DatedBond(100, .04, date(2024, 1, 1), date(2029, 1, 1), 1), 5),
        Position("SHORT8Y", DatedBond(100, .05, date(2024, 1, 1), date(2032, 1, 1), 1), -2),
    ))


def curve():
    return DiscountCurve.from_zero_rates((2, 5, 8), (.03, .04, .045))


def test_portfolio_report_aggregates_signed_positions_and_risk():
    report = portfolio_risk_report(portfolio(), SETTLEMENT, curve(), key_maturities=(2, 5, 8),
                                   scenarios=(steepener_scenario(long_maturity=8), twist_scenario(long_maturity=8)))
    assert report["base_market_value"] == pytest.approx(sum(row["market_value"] for row in report["positions"]))
    assert report["parallel_dv01"] == pytest.approx(sum(row["parallel_dv01"] for row in report["positions"]))
    for key, total in report["key_rate_dv01"].items():
        assert total == pytest.approx(sum(row["key_rate_dv01"][key] for row in report["positions"]))
    assert sum(report["key_rate_dv01"].values()) == pytest.approx(report["parallel_dv01"], rel=2e-5)
    assert report["effective_duration"] > 0
    assert report["effective_convexity"] > 0
    assert {row["name"] for row in report["scenarios"]} == {"steepener", "twist"}
    assert next(row for row in report["positions"] if row["identifier"] == "SHORT8Y")["market_value"] < 0


def test_zero_market_value_reports_undefined_normalized_risk():
    bond = DatedBond(100, 0, date(2024, 1, 1), date(2026, 1, 1), 1)
    p = Portfolio((Position("long", bond, 1), Position("short", bond, -1)))
    report = portfolio_risk_report(p, SETTLEMENT, curve(), key_maturities=(2, 5, 8))
    assert report["base_market_value"] == 0
    assert report["effective_duration"] is None
    assert report["effective_convexity"] is None


def test_load_portfolio_from_dataset():
    data = QuantDataset(pd.DataFrame({
        "identifier": ["A", "B"], "quantity": [3, -1], "face_value": [100, 100],
        "coupon_rate": [.03, .04], "issue_date": ["2024-01-01", "2024-01-01"],
        "maturity_date": ["2026-01-01", "2029-01-01"], "frequency": [1, 1],
        "day_count": ["ACT/ACT ISDA", "30E/360"],
    }))
    result = load_portfolio(data)
    assert len(result.positions) == 2
    assert result.positions[1].quantity == -1
    assert result.positions[1].bond.day_count is DayCount.THIRTY_E_360


def test_position_portfolio_dataset_and_report_validation():
    bond = DatedBond(100, 0, date(2024, 1, 1), date(2026, 1, 1), 1)
    with pytest.raises(ValueError):
        Position("", bond, 1)
    with pytest.raises(ValueError):
        Position("x", bond, 0)
    with pytest.raises(ValueError):
        Portfolio(())
    with pytest.raises(ValueError):
        Portfolio((Position("x", bond, 1), Position("x", bond, 2)))
    with pytest.raises(ValueError, match="missing"):
        load_portfolio(QuantDataset(pd.DataFrame({"identifier": ["x"]})))
    bad = QuantDataset(pd.DataFrame({
        "identifier": ["A"], "quantity": [1], "face_value": [100], "coupon_rate": [0],
        "issue_date": ["bad"], "maturity_date": ["2026-01-01"], "frequency": [1],
        "day_count": ["ACT/ACT ISDA"],
    }))
    with pytest.raises(ValueError, match="row 0"):
        load_portfolio(bad)
    for kwargs in [
        {"bump_bps": 0}, {"key_maturities": (5, 2)},
        {"scenarios": (CurveScenario("x", ((2, 1),)), CurveScenario("x", ((2, 2),)))},
    ]:
        with pytest.raises(ValueError):
            portfolio_risk_report(portfolio(), SETTLEMENT, curve(), **kwargs)

