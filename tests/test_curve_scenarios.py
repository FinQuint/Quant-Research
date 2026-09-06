from math import exp

import pytest

from finquint.fixed_income import DiscountCurve
from finquint.risk import (
    CurveScenario, flattener_scenario, key_rate_shift, parallel_shift,
    steepener_scenario, twist_scenario,
)


@pytest.fixture
def curve():
    return DiscountCurve.from_zero_rates((1, 2, 5, 10), (0.03, 0.035, 0.04, 0.045))


def test_parallel_shift_changes_continuous_zero_rates(curve):
    shifted = parallel_shift(curve, 25)
    for time in (0.5, 1, 3, 10):
        assert shifted.zero_rate(time) == pytest.approx(curve.zero_rate(time) + 0.0025)
        assert shifted.discount_factor(time) == pytest.approx(curve.discount_factor(time) * exp(-0.0025 * time))
    assert shifted.discount_factor(0) == 1


def test_key_rate_hat_weights_and_partition(curve):
    keys = (2, 5, 10)
    five = key_rate_shift(curve, 5, 100, keys)
    assert five.zero_rate(2) == pytest.approx(curve.zero_rate(2))
    assert five.zero_rate(5) == pytest.approx(curve.zero_rate(5) + .01)
    assert five.zero_rate(10) == pytest.approx(curve.zero_rate(10))
    assert five.zero_rate(3.5) == pytest.approx(curve.zero_rate(3.5) + .005)
    for time in (1, 2, 3.5, 5, 7.5, 10):
        total = sum(key_rate_shift(curve, key, 1, keys).zero_rate(time) - curve.zero_rate(time) for key in keys)
        assert total == pytest.approx(.0001)


def test_scenario_interpolation_and_presets(curve):
    scenario = CurveScenario("custom", ((2, -20), (5, 10), (10, 30)))
    assert scenario.shift_bps(1) == -20
    assert scenario.shift_bps(3.5) == pytest.approx(-5)
    assert scenario.shift_bps(20) == 30
    assert scenario.apply(curve).zero_rate(5) == pytest.approx(curve.zero_rate(5) + .001)
    assert steepener_scenario().shift_bps(2) < steepener_scenario().shift_bps(10)
    assert flattener_scenario().shift_bps(2) > flattener_scenario().shift_bps(10)
    assert twist_scenario().shift_bps(5) == 0


@pytest.mark.parametrize("call", [
    lambda c: parallel_shift(c, float("nan")),
    lambda c: key_rate_shift(c, 3, 1, (2, 5)),
    lambda c: key_rate_shift(c, 2, 1, (5, 2)),
    lambda c: CurveScenario("", ((2, 1),)),
    lambda c: CurveScenario("bad", ((2, 1), (2, 2))),
    lambda c: CurveScenario("bad", ((2, float("inf")),)),
])
def test_invalid_scenarios(curve, call):
    with pytest.raises((ValueError, TypeError)):
        call(curve)

