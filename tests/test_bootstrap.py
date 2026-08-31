from math import exp

import pytest

from finquint.fixed_income import Bond, BondQuote, DiscountCurve, bootstrap_bond_curve, price_bond_with_curve


def test_known_sequential_bootstrap():
    quotes = [BondQuote(Bond(100, 0, 0.5), 98), BondQuote(Bond(100, 0.04, 1), 99)]
    curve = bootstrap_bond_curve(reversed(quotes))
    assert curve.maturities == (0.5, 1)
    assert curve.discount_factor(0.5) == pytest.approx(0.98)
    assert curve.discount_factor(1) == pytest.approx((99 - 2 * 0.98) / 102)
    for quote in quotes:
        assert price_bond_with_curve(quote.bond, curve) == pytest.approx(quote.market_price, abs=1e-8)


@pytest.mark.parametrize("zeros", [(0.03, 0.04, 0.05), (-0.01, -0.015, -0.02), (0, 0, 0)])
def test_sparse_coupon_schedule_reprices_all_quotes(zeros):
    source = DiscountCurve.from_zero_rates((0.5, 2, 5), zeros)
    bonds = [Bond(100, 0, 0.5), Bond(100, 0.03, 2), Bond(100, 0.05, 5)]
    quotes = [BondQuote(b, price_bond_with_curve(b, source)) for b in bonds]
    calibrated = bootstrap_bond_curve(quotes)
    for time, rate in zip(source.maturities, zeros):
        assert calibrated.discount_factor(time) == pytest.approx(exp(-rate * time), abs=2e-10)
    for quote in quotes:
        assert price_bond_with_curve(quote.bond, calibrated) == pytest.approx(quote.market_price, abs=2e-8)


def test_first_pillar_may_have_intermediate_coupons():
    source = DiscountCurve.from_zero_rates((3,), (0.04,))
    bond = Bond(100, 0.06, 3)
    curve = bootstrap_bond_curve([BondQuote(bond, price_bond_with_curve(bond, source))])
    assert curve.zero_rate(3) == pytest.approx(0.04)


@pytest.mark.parametrize("price", [0, -1, float('nan'), float('inf')])
def test_invalid_prices(price):
    with pytest.raises(ValueError):
        BondQuote(Bond(100, 0.04, 1), price)


def test_bad_bootstrap_input():
    q = BondQuote(Bond(100, 0, 1), 95)
    for quotes in [[], [q, q], ["not a quote"]]:
        with pytest.raises(ValueError):
            bootstrap_bond_curve(quotes)
    for kwargs in [{"price_tolerance": 0}, {"price_tolerance": float('nan')}, {"max_iterations": 0}]:
        with pytest.raises(ValueError):
            bootstrap_bond_curve([q], **kwargs)
    with pytest.raises(RuntimeError, match="converge"):
        bootstrap_bond_curve([q], max_iterations=1)


def test_inconsistent_price_lower_than_known_coupons():
    with pytest.raises(ValueError, match="already-discounted coupons"):
        bootstrap_bond_curve([
            BondQuote(Bond(100, 0, 1, 1), 100),
            BondQuote(Bond(100, 0.10, 2, 1), 5),
        ])

