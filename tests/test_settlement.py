from datetime import date

import pytest

from finquint.fixed_income import (
    Bond, DatedBond, DayCount, accrued_interest, clean_price_from_yield,
    clean_to_dirty, dirty_price_from_yield, dirty_to_clean, price_bond,
    settlement_convexity, settlement_dv01, settlement_macaulay_duration,
    settlement_modified_duration, settlement_yield_to_maturity,
)


@pytest.fixture
def bond():
    return DatedBond(100, 0.05, date(2024, 1, 31), date(2026, 1, 31), 2, DayCount.THIRTY_E_360)


def test_accrued_interest_and_clean_dirty_round_trip(bond):
    settlement = date(2024, 4, 30)
    assert accrued_interest(bond, settlement) == pytest.approx(1.25)
    assert clean_to_dirty(bond, settlement, 99) == pytest.approx(100.25)
    assert dirty_to_clean(bond, settlement, 100.25) == pytest.approx(99)
    assert accrued_interest(bond, date(2024, 7, 31)) == 0


def test_coupon_date_price_matches_legacy_api(bond):
    settlement = date(2024, 7, 31)
    legacy = Bond(100, 0.05, 1.5, 2)
    assert dirty_price_from_yield(bond, settlement, 0.04) == pytest.approx(price_bond(legacy, 0.04))
    assert clean_price_from_yield(bond, settlement, 0.04) == pytest.approx(price_bond(legacy, 0.04))


@pytest.mark.parametrize("yield_rate", [-0.01, 0, 0.04, 0.12])
def test_ytm_round_trip_between_coupon_dates(bond, yield_rate):
    settlement = date(2024, 4, 30)
    dirty = dirty_price_from_yield(bond, settlement, yield_rate)
    assert settlement_yield_to_maturity(bond, settlement, dirty) == pytest.approx(yield_rate, abs=1e-10)


def test_settlement_risk_matches_price_bumps(bond):
    settlement, y, bump = date(2024, 4, 30), 0.04, 0.0001
    price = dirty_price_from_yield(bond, settlement, y)
    down = dirty_price_from_yield(bond, settlement, y - bump)
    up = dirty_price_from_yield(bond, settlement, y + bump)
    assert settlement_dv01(bond, settlement, y) == pytest.approx((down - up) / 2)
    assert settlement_convexity(bond, settlement, y) == pytest.approx((down - 2 * price + up) / (price * bump**2))
    assert settlement_modified_duration(bond, settlement, y) == pytest.approx(
        settlement_macaulay_duration(bond, settlement, y) / 1.02)


def test_invalid_settlement_price_yield_and_solver_options(bond):
    for settlement in [date(2023, 12, 31), bond.maturity_date]:
        with pytest.raises(ValueError):
            accrued_interest(bond, settlement)
    with pytest.raises(ValueError):
        clean_to_dirty(bond, date(2024, 4, 30), -1)
    with pytest.raises(ValueError):
        dirty_to_clean(bond, date(2024, 4, 30), 0.5)
    with pytest.raises(ValueError):
        dirty_price_from_yield(bond, date(2024, 4, 30), -2)
    for kwargs in [{"tolerance": 0}, {"max_iterations": 0}, {"max_iterations": 1.5}]:
        with pytest.raises(ValueError):
            settlement_yield_to_maturity(bond, date(2024, 4, 30), 100, **kwargs)
    with pytest.raises(RuntimeError):
        settlement_yield_to_maturity(bond, date(2024, 4, 30), 100, max_iterations=1)

