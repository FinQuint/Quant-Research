"""Transparent historical and parametric portfolio risk measures."""
from dataclasses import dataclass
from math import ceil, isfinite, log, sqrt
from statistics import NormalDist, fmean, stdev
from typing import Iterable, Mapping, Sequence


def _finite(values: Iterable[float], *, minimum=2, name="values") -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) < minimum or any(not isfinite(value) for value in result):
        raise ValueError(f"{name} requires at least {minimum} finite observations")
    return result


def _confidence(value: float) -> float:
    result = float(value)
    if not isfinite(result) or not 0 < result < 1:
        raise ValueError("confidence must lie strictly between zero and one")
    return result


def historical_var(pnl: Iterable[float], confidence: float = 0.99) -> float:
    """Positive loss threshold using an empirical linearly interpolated quantile."""
    losses, confidence = sorted(-value for value in _finite(pnl, name="pnl")), _confidence(confidence)
    position = confidence * (len(losses) - 1)
    lower, weight = int(position), position - int(position)
    quantile = losses[lower] if lower == len(losses) - 1 else losses[lower] * (1 - weight) + losses[lower + 1] * weight
    return max(0.0, quantile)


def expected_shortfall(pnl: Iterable[float], confidence: float = 0.99) -> float:
    """Average of the worst ceil((1-confidence)*n) historical losses."""
    losses, confidence = sorted((-value for value in _finite(pnl, name="pnl")), reverse=True), _confidence(confidence)
    count = max(1, ceil((1 - confidence) * len(losses)))
    return max(0.0, fmean(losses[:count]))


def parametric_var(pnl: Iterable[float], confidence: float = 0.99) -> float:
    values, confidence = _finite(pnl, name="pnl"), _confidence(confidence)
    return max(0.0, -fmean(values) + NormalDist().inv_cdf(confidence) * stdev(values))


def historical_changes(levels: Iterable[float], method: str = "difference") -> tuple[float, ...]:
    values = _finite(levels, name="levels")
    method = method.lower()
    if method == "difference":
        return tuple(b - a for a, b in zip(values, values[1:]))
    if method == "relative":
        if any(value == 0 for value in values[:-1]):
            raise ValueError("relative changes require nonzero prior levels")
        return tuple(b / a - 1 for a, b in zip(values, values[1:]))
    if method == "log":
        if any(value <= 0 for value in values):
            raise ValueError("log changes require positive levels")
        return tuple(log(b / a) for a, b in zip(values, values[1:]))
    raise ValueError("method must be difference, relative, or log")


def rolling_volatility(returns: Iterable[float], window: int, *, annualization: float = 1.0) -> tuple[float | None, ...]:
    values = _finite(returns, name="returns")
    if isinstance(window, bool) or not isinstance(window, int) or window < 2:
        raise ValueError("window must be an integer of at least two")
    if not isfinite(annualization) or annualization <= 0:
        raise ValueError("annualization must be finite and positive")
    scale, result = sqrt(annualization), []
    for index in range(len(values)):
        result.append(None if index + 1 < window else stdev(values[index + 1 - window:index + 1]) * scale)
    return tuple(result)


def covariance_matrix(observations: Iterable[Sequence[float]]) -> tuple[tuple[float, ...], ...]:
    rows = tuple(tuple(float(value) for value in row) for row in observations)
    if len(rows) < 2 or not rows or not rows[0]:
        raise ValueError("at least two nonempty observation rows are required")
    width = len(rows[0])
    if any(len(row) != width or any(not isfinite(v) for v in row) for row in rows):
        raise ValueError("observations must form a finite rectangular matrix")
    means = tuple(fmean(row[column] for row in rows) for column in range(width))
    return tuple(tuple(sum((row[i] - means[i]) * (row[j] - means[j]) for row in rows) / (len(rows) - 1)
                       for j in range(width)) for i in range(width))


def linear_pnl(exposures: Mapping[str, float], shocks: Iterable[Mapping[str, float]]) -> tuple[float, ...]:
    if not exposures or any(not isfinite(float(value)) for value in exposures.values()):
        raise ValueError("exposures must be nonempty and finite")
    result = []
    for shock in shocks:
        missing = set(exposures) - set(shock)
        if missing:
            raise ValueError(f"shock is missing factors: {', '.join(sorted(missing))}")
        values = [float(shock[key]) for key in exposures]
        if any(not isfinite(value) for value in values):
            raise ValueError("shock values must be finite")
        result.append(sum(float(exposures[key]) * float(shock[key]) for key in exposures))
    if not result:
        raise ValueError("at least one shock is required")
    return tuple(result)


@dataclass(frozen=True)
class StressScenario:
    name: str
    shocks: Mapping[str, float]

    def __post_init__(self):
        if not self.name.strip() or not self.shocks or any(not isfinite(float(v)) for v in self.shocks.values()):
            raise ValueError("stress scenario requires a name and finite shocks")


def stress_test(exposures: Mapping[str, float], scenarios: Iterable[StressScenario]) -> tuple[dict, ...]:
    values = tuple(scenarios)
    if not values or any(not isinstance(value, StressScenario) for value in values):
        raise ValueError("provide StressScenario objects")
    if len({value.name for value in values}) != len(values):
        raise ValueError("stress scenario names must be unique")
    return tuple({"name": scenario.name, "pnl": linear_pnl(exposures, (scenario.shocks,))[0],
                  "shocks": dict(scenario.shocks)} for scenario in values)


def backtest_var(actual_pnl: Iterable[float], var: float | Iterable[float], *, labels=None) -> dict:
    pnl = _finite(actual_pnl, name="actual_pnl")
    limits = (float(var),) * len(pnl) if isinstance(var, (int, float)) else tuple(float(v) for v in var)
    if len(limits) != len(pnl) or any(not isfinite(v) or v < 0 for v in limits):
        raise ValueError("VaR limits must be finite, nonnegative, and align with P&L")
    names = tuple(range(len(pnl))) if labels is None else tuple(labels)
    if len(names) != len(pnl):
        raise ValueError("labels must align with P&L")
    breaches = tuple(names[i] for i, (value, limit) in enumerate(zip(pnl, limits)) if -value > limit)
    return {"observations": len(pnl), "breach_count": len(breaches),
            "breach_rate": len(breaches) / len(pnl), "breaches": breaches}
