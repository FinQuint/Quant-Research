from datetime import date

import pytest

from finquint.fixed_income import DatedBond, DayCount
from finquint.fixed_income.stages import DatedBondAnalyticsStage
from finquint.pipeline import QuantPipeline


def test_dated_stage_from_clean_price_preserves_data():
    bond = DatedBond(100, .05, date(2024, 1, 31), date(2026, 1, 31), 2, DayCount.THIRTY_E_360)
    marker = object()
    result = QuantPipeline().add(DatedBondAnalyticsStage(
        bond, date(2024, 4, 30), clean_price=99)).run(marker)
    assert result.data is marker
    assert result.context.get("dated_accrued_interest") == pytest.approx(1.25)
    assert result.context.get("dated_dirty_price") == pytest.approx(100.25)
    assert {"dated_ytm", "dated_dv01", "dated_convexity"} <= result.context.results.keys()


def test_stage_from_yield_and_constructor_contract():
    bond = DatedBond(100, .05, date(2024, 1, 31), date(2025, 1, 31), 2)
    result = QuantPipeline().add(DatedBondAnalyticsStage(
        bond, date(2024, 3, 31), annual_yield=.04)).run()
    assert result.context.get("dated_clean_price") < result.context.get("dated_dirty_price")
    for kwargs in [{}, {"clean_price": 99, "dirty_price": 100}]:
        with pytest.raises(ValueError, match="exactly one"):
            DatedBondAnalyticsStage(bond, date(2024, 3, 31), **kwargs)
