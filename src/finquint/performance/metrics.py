"""Transparent performance statistics for periodic return series."""
from dataclasses import asdict, dataclass
from math import isfinite, sqrt
from statistics import NormalDist

import pandas as pd


def validate_returns(values, *, name="returns") -> pd.Series:
    series = pd.Series(values, dtype=float).dropna()
    if series.empty or not series.map(isfinite).all():
        raise ValueError(f"{name} must contain finite observations")
    if (series <= -1).any():
        raise ValueError(f"{name} cannot contain returns at or below -100%")
    return series


def returns_from_equity(equity) -> pd.Series:
    series = pd.Series(equity, dtype=float)
    if len(series) < 2 or series.isna().any() or not series.map(isfinite).all() or (series <= 0).any():
        raise ValueError("equity must contain at least two finite positive values")
    return series.pct_change().dropna()


def annualized_return(returns, periods_per_year=252) -> float:
    series = validate_returns(returns)
    return float((1 + series).prod() ** (periods_per_year / len(series)) - 1)


def annualized_volatility(returns, periods_per_year=252) -> float:
    series = validate_returns(returns)
    return float(series.std(ddof=1) * sqrt(periods_per_year)) if len(series) > 1 else 0.0


def drawdown_series(returns) -> pd.Series:
    series = validate_returns(returns)
    wealth = (1 + series).cumprod()
    return wealth / wealth.cummax() - 1


def mean_return_confidence_interval(returns, confidence=0.95, periods_per_year=252):
    series = validate_returns(returns)
    if not 0 < confidence < 1 or len(series) < 2:
        raise ValueError("confidence must be between zero and one and at least two returns are required")
    standard_error = series.std(ddof=1) / sqrt(len(series))
    z_score = NormalDist().inv_cdf(0.5 + confidence / 2)
    mean = series.mean()
    return float((mean - z_score * standard_error) * periods_per_year), float((mean + z_score * standard_error) * periods_per_year)


@dataclass(frozen=True)
class PerformanceSummary:
    observations: int
    total_return: float
    annualized_return: float
    annualized_volatility: float
    sharpe_ratio: float | None
    sortino_ratio: float | None
    maximum_drawdown: float
    calmar_ratio: float | None
    tracking_error: float | None
    information_ratio: float | None
    beta: float | None
    alpha: float | None
    mean_return_ci_low: float
    mean_return_ci_high: float

    def to_dict(self):
        return asdict(self)


def summarize_performance(returns, *, benchmark_returns=None, risk_free_rate=0.0,
                          periods_per_year=252, confidence=0.95) -> PerformanceSummary:
    series = validate_returns(returns)
    if periods_per_year < 1 or not isfinite(risk_free_rate):
        raise ValueError("periods_per_year must be positive and risk_free_rate finite")
    annual_return = annualized_return(series, periods_per_year)
    annual_vol = annualized_volatility(series, periods_per_year)
    rf_period = (1 + risk_free_rate) ** (1 / periods_per_year) - 1
    excess = series - rf_period
    sharpe = float(excess.mean() / series.std(ddof=1) * sqrt(periods_per_year)) if len(series) > 1 and series.std(ddof=1) > 0 else None
    downside = excess[excess < 0]
    downside_deviation = float(sqrt((downside.pow(2).sum() / len(series))) * sqrt(periods_per_year))
    sortino = float(excess.mean() * periods_per_year / downside_deviation) if downside_deviation > 0 else None
    max_drawdown = float(drawdown_series(series).min())
    calmar = float(annual_return / abs(max_drawdown)) if max_drawdown < 0 else None
    tracking_error = information = beta = alpha = None
    if benchmark_returns is not None:
        benchmark = validate_returns(benchmark_returns, name="benchmark_returns")
        if len(benchmark) != len(series):
            raise ValueError("strategy and benchmark returns must have equal length")
        benchmark.index = series.index
        active = series - benchmark
        active_std = active.std(ddof=1)
        tracking_error = float(active_std * sqrt(periods_per_year)) if len(active) > 1 else 0.0
        information = float(active.mean() / active_std * sqrt(periods_per_year)) if active_std > 0 else None
        benchmark_variance = benchmark.var(ddof=1)
        if benchmark_variance > 0:
            beta = float(series.cov(benchmark) / benchmark_variance)
            alpha = float((series.mean() - rf_period - beta * (benchmark.mean() - rf_period)) * periods_per_year)
    ci_low, ci_high = mean_return_confidence_interval(series, confidence, periods_per_year)
    return PerformanceSummary(len(series), float((1 + series).prod() - 1), annual_return,
                              annual_vol, sharpe, sortino, max_drawdown, calmar,
                              tracking_error, information, beta, alpha, ci_low, ci_high)


def rolling_performance(returns, window, *, risk_free_rate=0.0, periods_per_year=252) -> pd.DataFrame:
    series = validate_returns(returns)
    if not isinstance(window, int) or window < 2:
        raise ValueError("window must be an integer of at least two")
    rows = []
    for end in range(window, len(series) + 1):
        subset = series.iloc[end - window:end]
        summary = summarize_performance(subset, risk_free_rate=risk_free_rate,
                                        periods_per_year=periods_per_year)
        rows.append({"index": series.index[end - 1], "annualized_return": summary.annualized_return,
                     "annualized_volatility": summary.annualized_volatility,
                     "sharpe_ratio": summary.sharpe_ratio,
                     "maximum_drawdown": summary.maximum_drawdown})
    return pd.DataFrame(rows).set_index("index") if rows else pd.DataFrame(
        columns=["annualized_return", "annualized_volatility", "sharpe_ratio", "maximum_drawdown"])


def subperiod_performance(returns, labels, *, periods_per_year=252) -> pd.DataFrame:
    series = validate_returns(returns)
    label_series = pd.Series(labels, index=series.index)
    if len(label_series) != len(series) or label_series.isna().any():
        raise ValueError("each return must have a subperiod label")
    rows = []
    for label, indices in label_series.groupby(label_series).groups.items():
        summary = summarize_performance(series.loc[indices], periods_per_year=periods_per_year)
        rows.append({"subperiod": label, **summary.to_dict()})
    return pd.DataFrame(rows)
