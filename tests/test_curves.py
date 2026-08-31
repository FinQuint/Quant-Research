from dataclasses import FrozenInstanceError
from math import exp, log, sqrt

import pytest

from finquint.fixed_income import Bond, DiscountCurve, price_bond, price_bond_with_curve


def test_log_linear_interpolation_and_rates():
    curve = DiscountCurve((1, 2), (0.96, 0.90))
    assert curve.discount_factor(0) == 1
    assert curve.discount_factor(1) == 0.96
    assert curve.discount_factor(2) == 0.90
    assert curve.discount_factor(0.5) == pytest.approx(sqrt(0.96))
    assert curve.discount_factor(1.5) == pytest.approx(sqrt(0.96 * 0.90))
    assert curve.zero_rate(2) == pytest.approx(-log(0.90) / 2)
    assert curve.forward_rate(1, 2) == pytest.approx(log(0.96 / 0.90))
    assert curve.forward_rate(1, 1.5) == pytest.approx(curve.forward_rate(1.5, 2))
    assert exp(-curve.forward_rate(0, 2) * 2) == pytest.approx(0.90)


@pytest.mark.parametrize("rate", [-0.02, 0, 0.03])
def test_flat_zero_curve(rate):
    curve = DiscountCurve.from_zero_rates((1, 3), (rate, rate))
    assert curve.zero_rate(2) == pytest.approx(rate)
    assert curve.forward_rate(0.5, 2.5) == pytest.approx(rate)
    bond = Bond(100, 0, 3, 1)
    assert price_bond_with_curve(bond, curve) == pytest.approx(100 * exp(-rate * 3))


def test_curve_pricing_matches_legacy_nominal_yield():
    bond = Bond(100, 0.05, 5, 2)
    continuous = 2 * log(1 + 0.04 / 2)
    curve = DiscountCurve.from_zero_rates((5,), (continuous,))
    assert price_bond_with_curve(bond, curve) == pytest.approx(price_bond(bond, 0.04))


@pytest.mark.parametrize("times,discounts", [
    ((), ()), ((1,), ()), ((0,), (1,)), ((-1,), (1,)),
    ((2, 1), (0.9, 0.8)), ((1, 1), (0.9, 0.8)),
    ((float('nan'),), (1,)), ((1,), (float('inf'),)), ((1,), (0,)), ((1,), (-1,)),
])
def test_invalid_curve_nodes(times, discounts):
    with pytest.raises(ValueError):
        DiscountCurve(times, discounts)


@pytest.mark.parametrize("time", [-1, float('nan'), float('inf'), 2.01])
def test_queries_outside_domain_fail(time):
    with pytest.raises(ValueError):
        DiscountCurve((2,), (0.9,)).discount_factor(time)


def test_invalid_rate_queries_and_extrapolated_bond():
    curve = DiscountCurve((2,), (0.9,))
    with pytest.raises(ValueError):
        curve.zero_rate(0)
    for start, end in [(1, 1), (2, 1), (-1, 1), (0, float('nan'))]:
        with pytest.raises(ValueError):
            curve.forward_rate(start, end)
    with pytest.raises(ValueError, match="extrapolation"):
        price_bond_with_curve(Bond(100, 0.05, 3), curve)
    with pytest.raises(ValueError):
        DiscountCurve.from_zero_rates((1, 2), (0.05,))


def test_curve_copies_inputs_and_is_immutable():
    times, dfs = [1, 2], [0.98, 0.95]
    curve = DiscountCurve(times, dfs)
    dfs[0] = 0.1
    assert curve.discount_factor(1) == 0.98
    with pytest.raises(FrozenInstanceError):
        curve.maturities = (4,)

