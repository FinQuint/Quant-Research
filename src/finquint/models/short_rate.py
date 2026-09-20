"""One-factor Vasicek and CIR short-rate models."""
from dataclasses import dataclass
from math import exp, isfinite, log, sqrt
from random import Random
from statistics import fmean
from typing import Iterable


def _positive(name: str, value: float) -> float:
    value = float(value)
    if not isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


def _simulation_inputs(initial_rate, steps, dt, paths):
    rate, dt = float(initial_rate), _positive("dt", dt)
    if not isfinite(rate):
        raise ValueError("initial_rate must be finite")
    if isinstance(steps, bool) or not isinstance(steps, int) or steps < 1:
        raise ValueError("steps must be a positive integer")
    if isinstance(paths, bool) or not isinstance(paths, int) or paths < 1:
        raise ValueError("paths must be a positive integer")
    return rate, dt


@dataclass(frozen=True)
class VasicekModel:
    mean_reversion: float
    long_run_rate: float
    volatility: float

    def __post_init__(self):
        object.__setattr__(self, "mean_reversion", _positive("mean_reversion", self.mean_reversion))
        if not isfinite(self.long_run_rate):
            raise ValueError("long_run_rate must be finite")
        object.__setattr__(self, "volatility", _positive("volatility", self.volatility))

    def zero_coupon_price(self, current_rate: float, maturity: float) -> float:
        r, t, k, theta, sigma = float(current_rate), float(maturity), self.mean_reversion, self.long_run_rate, self.volatility
        if not isfinite(r) or not isfinite(t) or t < 0:
            raise ValueError("rate must be finite and maturity nonnegative")
        if t == 0:
            return 1.0
        b = (1 - exp(-k * t)) / k
        a = exp((theta - sigma * sigma / (2 * k * k)) * (b - t) - sigma * sigma * b * b / (4 * k))
        return a * exp(-b * r)

    def simulate(self, initial_rate: float, steps: int, dt: float, *, paths: int = 1, seed: int | None = None):
        initial_rate, dt = _simulation_inputs(initial_rate, steps, dt, paths)
        rng, decay = Random(seed), exp(-self.mean_reversion * dt)
        stdev = self.volatility * sqrt((1 - decay * decay) / (2 * self.mean_reversion))
        result = []
        for _ in range(paths):
            values = [initial_rate]
            for _ in range(steps):
                mean = self.long_run_rate + (values[-1] - self.long_run_rate) * decay
                values.append(mean + stdev * rng.gauss(0, 1))
            result.append(tuple(values))
        return tuple(result)


@dataclass(frozen=True)
class CIRModel:
    mean_reversion: float
    long_run_rate: float
    volatility: float

    def __post_init__(self):
        object.__setattr__(self, "mean_reversion", _positive("mean_reversion", self.mean_reversion))
        object.__setattr__(self, "long_run_rate", _positive("long_run_rate", self.long_run_rate))
        object.__setattr__(self, "volatility", _positive("volatility", self.volatility))

    @property
    def feller_satisfied(self) -> bool:
        return 2 * self.mean_reversion * self.long_run_rate >= self.volatility ** 2

    def zero_coupon_price(self, current_rate: float, maturity: float) -> float:
        r, t = float(current_rate), float(maturity)
        if not isfinite(r) or r < 0 or not isfinite(t) or t < 0:
            raise ValueError("CIR rate and maturity must be nonnegative")
        if t == 0:
            return 1.0
        k, theta, sigma = self.mean_reversion, self.long_run_rate, self.volatility
        gamma = sqrt(k * k + 2 * sigma * sigma)
        growth = exp(gamma * t)
        denominator = 2 * gamma + (k + gamma) * (growth - 1)
        b = 2 * (growth - 1) / denominator
        a = (2 * gamma * exp((k + gamma) * t / 2) / denominator) ** (2 * k * theta / (sigma * sigma))
        return a * exp(-b * r)

    def simulate(self, initial_rate: float, steps: int, dt: float, *, paths: int = 1, seed: int | None = None):
        initial_rate, dt = _simulation_inputs(initial_rate, steps, dt, paths)
        if initial_rate < 0:
            raise ValueError("CIR initial_rate must be nonnegative")
        rng, root_dt, result = Random(seed), sqrt(dt), []
        for _ in range(paths):
            values = [initial_rate]
            for _ in range(steps):
                prior = max(values[-1], 0.0)
                next_rate = values[-1] + self.mean_reversion * (self.long_run_rate - prior) * dt + self.volatility * sqrt(prior) * root_dt * rng.gauss(0, 1)
                values.append(max(next_rate, 0.0))
            result.append(tuple(values))
        return tuple(result)


def _observations(values: Iterable[float], dt: float):
    data, dt = tuple(float(v) for v in values), _positive("dt", dt)
    if len(data) < 3 or any(not isfinite(v) for v in data):
        raise ValueError("at least three finite observations are required")
    return data, dt


def calibrate_vasicek(values: Iterable[float], dt: float) -> VasicekModel:
    data, dt = _observations(values, dt)
    x, y = data[:-1], data[1:]
    xbar, ybar = fmean(x), fmean(y)
    variance = sum((v - xbar) ** 2 for v in x)
    if variance <= 0:
        raise ValueError("observations have no variation")
    beta = sum((a - xbar) * (b - ybar) for a, b in zip(x, y)) / variance
    if not 0 < beta < 1:
        raise ValueError("observations do not imply positive mean reversion")
    intercept, k = ybar - beta * xbar, -log(beta) / dt
    theta = intercept / (1 - beta)
    residual_variance = sum((b - intercept - beta * a) ** 2 for a, b in zip(x, y)) / len(x)
    sigma = sqrt(max(residual_variance * 2 * k / (1 - beta * beta), 1e-18))
    return VasicekModel(k, theta, sigma)


def calibrate_cir(values: Iterable[float], dt: float) -> CIRModel:
    data, dt = _observations(values, dt)
    if any(v < 0 for v in data):
        raise ValueError("CIR observations must be nonnegative")
    x = data[:-1]
    changes = [(b - a) / dt for a, b in zip(data, data[1:])]
    xbar, ybar = fmean(x), fmean(changes)
    variance = sum((v - xbar) ** 2 for v in x)
    if variance <= 0:
        raise ValueError("observations have no variation")
    slope = sum((a - xbar) * (b - ybar) for a, b in zip(x, changes)) / variance
    k = -slope
    if k <= 0:
        raise ValueError("observations do not imply positive mean reversion")
    intercept, theta = ybar - slope * xbar, (ybar - slope * xbar) / k
    if theta <= 0:
        raise ValueError("observations do not imply a positive long-run rate")
    residuals = [(b - a) - k * (theta - a) * dt for a, b in zip(data, data[1:])]
    denominator = sum(max(a, 1e-12) * dt for a in x)
    sigma = sqrt(max(sum(e * e for e in residuals) / denominator, 1e-18))
    return CIRModel(k, theta, sigma)
