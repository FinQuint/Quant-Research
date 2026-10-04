import math

import pandas as pd
import pytest

from finquint.performance import (
    annualized_return,
    annualized_volatility,
    drawdown_series,
    mean_return_confidence_interval,
    returns_from_equity,
    rolling_performance,
    subperiod_performance,
    summarize_performance,
)


def sample_returns():
    return pd.Series([0.01, -0.005, 0.012, -0.003, 0.008, 0.004],
                     index=pd.date_range("2025-01-01", periods=6))


def test_returns_from_equity_uses_period_changes():
    result = returns_from_equity([100, 101, 99.99])
    assert result.tolist() == pytest.approx([0.01, -0.01])


def test_annualized_metrics_match_transparent_formulas():
    returns = sample_returns()
    assert annualized_return(returns, 12) == pytest.approx((1 + returns).prod() ** 2 - 1)
    assert annualized_volatility(returns, 12) == pytest.approx(returns.std(ddof=1) * math.sqrt(12))


def test_drawdown_tracks_peak_to_trough_loss():
    drawdowns = drawdown_series(pd.Series([0.10, -0.20, 0.05]))
    assert drawdowns.iloc[0] == 0
    assert drawdowns.min() == pytest.approx(-0.20)


def test_summary_includes_absolute_and_risk_adjusted_statistics():
    summary = summarize_performance(sample_returns(), risk_free_rate=0.02, periods_per_year=12)
    assert summary.observations == 6
    assert summary.total_return == pytest.approx((1 + sample_returns()).prod() - 1)
    assert summary.sharpe_ratio is not None
    assert summary.sortino_ratio is not None
    assert summary.maximum_drawdown < 0
    assert summary.mean_return_ci_low < summary.mean_return_ci_high


def test_benchmark_relative_metrics_are_reported():
    strategy = sample_returns()
    benchmark = pd.Series([0.005, -0.002, 0.006, -0.001, 0.004, 0.002])
    summary = summarize_performance(strategy, benchmark_returns=benchmark, periods_per_year=12)
    assert summary.tracking_error > 0
    assert summary.information_ratio is not None
    assert summary.beta is not None
    assert summary.alpha is not None


def test_equal_length_is_required_for_benchmark():
    with pytest.raises(ValueError, match="equal length"):
        summarize_performance(sample_returns(), benchmark_returns=[0.01, 0.02])


def test_confidence_interval_requires_multiple_observations():
    with pytest.raises(ValueError, match="at least two"):
        mean_return_confidence_interval([0.01])


def test_rolling_performance_has_one_row_per_complete_window():
    rolling = rolling_performance(sample_returns(), 3, periods_per_year=12)
    assert len(rolling) == 4
    assert set(rolling.columns) == {"annualized_return", "annualized_volatility",
                                    "sharpe_ratio", "maximum_drawdown"}


def test_subperiod_summary_keeps_regimes_separate():
    result = subperiod_performance(sample_returns(), ["early"] * 3 + ["late"] * 3,
                                   periods_per_year=12)
    assert set(result["subperiod"]) == {"early", "late"}
    assert set(result["observations"]) == {3}


@pytest.mark.parametrize("values", [[0.01, -1.0], [100.0], [0.01, float("inf")]])
def test_invalid_return_or_equity_inputs_are_rejected(values):
    if values == [100.0]:
        with pytest.raises(ValueError):
            returns_from_equity(values)
    else:
        with pytest.raises(ValueError):
            summarize_performance(values)
