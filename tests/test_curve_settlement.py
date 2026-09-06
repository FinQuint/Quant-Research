from datetime import date
from math import exp

import pytest

from finquint.fixed_income import (
    DatedBond, DayCount, DiscountCurve, accrued_interest,
    clean_price_with_curve, dirty_price_with_curve,
)


def test_zero_coupon_dated_curve_price():
    bond = DatedBond(100, 0, date(2024, 1, 1), date(2026, 1, 1), 1)
    curve = DiscountCurve.from_zero_rates((2,), (0.04,))
    assert dirty_price_with_curve(bond, date(2024, 1, 1), curve) == pytest.approx(100 * exp(-.04 * 2))


def test_clean_and_dirty_curve_prices_differ_by_accrual():
    bond = DatedBond(100, .06, date(2024, 1, 31), date(2026, 1, 31), 2, DayCount.THIRTY_E_360)
    settlement = date(2024, 4, 30)
    curve = DiscountCurve.from_zero_rates((2,), (.04,))
    dirty = dirty_price_with_curve(bond, settlement, curve, curve_day_count=DayCount.THIRTY_E_360)
    clean = clean_price_with_curve(bond, settlement, curve, curve_day_count=DayCount.THIRTY_E_360)
    assert dirty - clean == pytest.approx(accrued_interest(bond, settlement))


def test_curve_must_cover_last_payment():
    bond = DatedBond(100, .05, date(2024, 1, 1), date(2029, 1, 1), 1)
    with pytest.raises(ValueError, match="extrapolation"):
        dirty_price_with_curve(bond, date(2024, 1, 1), DiscountCurve((2,), (.9,)))
    with pytest.raises(TypeError):
        dirty_price_with_curve(bond, date(2024, 1, 1), object())

