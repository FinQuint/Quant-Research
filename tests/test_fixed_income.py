import pytest

from finquint.fixed_income import (
    Bond,
    convexity,
    dv01,
    macaulay_duration,
    modified_duration,
    price_bond,
    yield_to_maturity,
)


@pytest.fixture
def par_bond():
    return Bond(face_value=100, coupon_rate=0.05, maturity_years=5, frequency=2)


def test_par_bond_price_and_yield(par_bond):
    assert price_bond(par_bond, 0.05) == pytest.approx(100.0)
    assert yield_to_maturity(par_bond, 100.0) == pytest.approx(0.05, abs=1e-10)


def test_known_price_and_yield_round_trip(par_bond):
    market_price = price_bond(par_bond, 0.04)
    assert market_price == pytest.approx(104.4912925)
    assert yield_to_maturity(par_bond, market_price) == pytest.approx(0.04, abs=1e-10)


def test_duration_dv01_and_convexity(par_bond):
    assert macaulay_duration(par_bond, 0.05) == pytest.approx(4.4854328)
    assert modified_duration(par_bond, 0.05) == pytest.approx(4.3760320)
    assert dv01(par_bond, 0.05) == pytest.approx(0.04376033)
    assert convexity(par_bond, 0.05) == pytest.approx(22.6123222)


def test_zero_coupon_duration():
    bond = Bond(100, 0, 3, 1)
    assert macaulay_duration(bond, 0.06) == pytest.approx(3.0)
    assert modified_duration(bond, 0.06) == pytest.approx(3 / 1.06)


def test_invalid_bond_and_market_price():
    with pytest.raises(ValueError):
        Bond(0, 0.05, 5)
    with pytest.raises(ValueError):
        yield_to_maturity(Bond(100, 0.05, 5), 0)
