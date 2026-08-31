from bisect import bisect_left
from dataclasses import dataclass, field
from math import exp, isfinite, log
from typing import Iterable

from .base import YieldCurve


@dataclass(frozen=True)
class DiscountCurve(YieldCurve):
    """Immutable log-linear discount curve, anchored at D(0)=1.

    Pillars must be strictly increasing and positive. Negative rates are
    supported (discount factors may exceed one). Extrapolation is rejected.
    Between pillars this interpolation implies constant forward rates.
    """

    maturities: tuple[float, ...]
    discount_factors: tuple[float, ...]
    _times: tuple[float, ...] = field(init=False, repr=False)
    _logs: tuple[float, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        times = tuple(float(t) for t in self.maturities)
        dfs = tuple(float(d) for d in self.discount_factors)
        if not times or len(times) != len(dfs):
            raise ValueError("provide nonempty, equal-length maturities and discount factors")
        if any(not isfinite(t) or t <= 0 for t in times):
            raise ValueError("pillar maturities must be finite and positive")
        if any(b <= a for a, b in zip(times, times[1:])):
            raise ValueError("pillar maturities must be strictly increasing")
        if any(not isfinite(d) or d <= 0 for d in dfs):
            raise ValueError("discount factors must be finite and positive")
        object.__setattr__(self, "maturities", times)
        object.__setattr__(self, "discount_factors", dfs)
        object.__setattr__(self, "_times", (0.0,) + times)
        object.__setattr__(self, "_logs", (0.0,) + tuple(log(d) for d in dfs))

    @classmethod
    def from_zero_rates(cls, maturities: Iterable[float], rates: Iterable[float]) -> "DiscountCurve":
        """Construct from continuously compounded annual zero rates, not YTMs."""
        times, zeros = tuple(maturities), tuple(rates)
        if len(times) != len(zeros) or any(not isfinite(z) for z in zeros):
            raise ValueError("provide one finite zero rate per maturity")
        try:
            return cls(times, tuple(exp(-t * z) for t, z in zip(times, zeros)))
        except OverflowError as exc:
            raise ValueError("zero rates imply unrepresentable discount factors") from exc

    def discount_factor(self, maturity: float) -> float:
        if not isfinite(maturity) or maturity < 0:
            raise ValueError("maturity must be finite and nonnegative")
        if maturity == 0:
            return 1.0
        if maturity > self.maturities[-1]:
            raise ValueError("maturity exceeds last pillar; extrapolation is disabled")
        index = bisect_left(self._times, maturity)
        if self._times[index] == maturity:
            return self.discount_factors[index - 1]
        left, right = self._times[index - 1:index + 1]
        weight = (maturity - left) / (right - left)
        return exp((1 - weight) * self._logs[index - 1] + weight * self._logs[index])

