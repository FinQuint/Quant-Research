import pytest

from finquint.risk import (
    StressScenario, backtest_var, covariance_matrix, expected_shortfall,
    historical_changes, historical_var, linear_pnl, parametric_var,
    rolling_volatility, stress_test,
)


def test_var_and_expected_shortfall_sign_conventions():
    pnl = (-10, -5, 0, 5, 10)
    assert historical_var(pnl, 0.8) == pytest.approx(6)
    assert expected_shortfall(pnl, 0.8) == 10
    assert parametric_var(pnl, 0.95) > 0


def test_gains_only_have_zero_reported_var():
    assert historical_var((1, 2, 3), 0.95) == 0
    assert expected_shortfall((1, 2, 3), 0.95) == 0


def test_historical_change_methods():
    assert historical_changes((100, 110, 99), "difference") == (10, -11)
    assert historical_changes((100, 110), "relative") == pytest.approx((0.1,))
    assert historical_changes((100, 110), "log")[0] == pytest.approx(0.09531018)
    with pytest.raises(ValueError):
        historical_changes((0, 1), "relative")


def test_rolling_volatility_and_covariance():
    vol = rolling_volatility((1, 2, 3, 4), 3, annualization=4)
    assert vol[:2] == (None, None)
    assert vol[2] == pytest.approx(2)
    matrix = covariance_matrix(((1, 2), (2, 4), (3, 6)))
    assert matrix[0] == pytest.approx((1, 2))
    assert matrix[1] == pytest.approx((2, 4))


def test_linear_pnl_and_stress_testing():
    exposures = {"rates_5y": -4000, "equity": 10000}
    shocks = ({"rates_5y": 0.01, "equity": -0.02}, {"rates_5y": -0.01, "equity": 0.01})
    assert linear_pnl(exposures, shocks) == pytest.approx((-240, 140))
    reports = stress_test(exposures, (StressScenario("risk_off", shocks[0]),))
    assert reports[0]["pnl"] == pytest.approx(-240)
    with pytest.raises(ValueError, match="missing"):
        linear_pnl(exposures, ({"equity": -0.1},))


def test_var_backtest_reports_breach_labels():
    report = backtest_var((-1, -6, 2, -8), 5, labels=("a", "b", "c", "d"))
    assert report["breach_count"] == 2
    assert report["breaches"] == ("b", "d")
    assert report["breach_rate"] == 0.5


def test_historical_risk_validation():
    with pytest.raises(ValueError):
        historical_var((1,), 0.99)
    with pytest.raises(ValueError):
        expected_shortfall((1, 2), 1)
    with pytest.raises(ValueError):
        rolling_volatility((1, 2), 1)
    with pytest.raises(ValueError):
        covariance_matrix(((1, 2), (3,)))
