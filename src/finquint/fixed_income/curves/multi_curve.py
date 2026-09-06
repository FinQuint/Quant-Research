"""Separate discounting and forward-projection curves."""
from dataclasses import dataclass
from math import isfinite

from .base import YieldCurve


@dataclass(frozen=True)
class MultiCurveSet:
    discount_curve: YieldCurve
    projection_curve: YieldCurve

    def __post_init__(self):
        if not isinstance(self.discount_curve, YieldCurve) or not isinstance(self.projection_curve, YieldCurve):
            raise TypeError("discount and projection curves must implement YieldCurve")

    def discount_factor(self, maturity: float) -> float:
        return self.discount_curve.discount_factor(maturity)

    def forward_rate(self, start: float, end: float, *, accrual: float | None = None) -> float:
        if not isfinite(start) or not isfinite(end) or not 0 <= start < end:
            raise ValueError("forward interval must satisfy 0 <= start < end")
        year_fraction = end - start if accrual is None else float(accrual)
        if not isfinite(year_fraction) or year_fraction <= 0:
            raise ValueError("accrual must be finite and positive")
        return (self.projection_curve.discount_factor(start) / self.projection_curve.discount_factor(end) - 1) / year_fraction
