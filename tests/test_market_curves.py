import pytest

from finquint.fixed_income import (
    DepositQuote, DiscountCurve, MultiCurveSet, SwapQuote,
    bootstrap_market_curve, par_swap_rate,
)


def test_deposit_discount_factor_and_swap_repricing():
    quotes = (
        DepositQuote(0.5, 0.04),
        SwapQuote(1.0, 0.042, 2),
        SwapQuote(2.0, 0.045, 2),
        SwapQuote(3.0, 0.047, 2),
    )
    curve = bootstrap_market_curve(quotes)
    assert curve.discount_factor(0.5) == pytest.approx(1 / 1.02)
    for quote in quotes[1:]:
        assert par_swap_rate(curve, quote.maturity, quote.frequency) == pytest.approx(quote.fixed_rate, abs=1e-10)


def test_negative_deposit_rate_is_supported():
    curve = bootstrap_market_curve((DepositQuote(1, -0.005),))
    assert curve.discount_factor(1) > 1


def test_market_quote_validation():
    with pytest.raises(ValueError):
        DepositQuote(1, -1)
    with pytest.raises(ValueError):
        SwapQuote(1.1, 0.04, 2)
    with pytest.raises(ValueError):
        bootstrap_market_curve((DepositQuote(1, 0.03), SwapQuote(1, 0.04)))


def test_multi_curve_uses_projection_for_forward_and_discount_for_pv():
    discount = DiscountCurve.from_zero_rates((1, 2), (0.03, 0.035))
    projection = DiscountCurve.from_zero_rates((1, 2), (0.04, 0.045))
    curves = MultiCurveSet(discount, projection)
    assert curves.discount_factor(2) == discount.discount_factor(2)
    expected = projection.discount_factor(1) / projection.discount_factor(2) - 1
    assert curves.forward_rate(1, 2) == pytest.approx(expected)


def test_multi_curve_validation():
    curve = DiscountCurve.from_zero_rates((1,), (0.03,))
    with pytest.raises(TypeError):
        MultiCurveSet(curve, object())
    with pytest.raises(ValueError):
        MultiCurveSet(curve, curve).forward_rate(1, 1)
