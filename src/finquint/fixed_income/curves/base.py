"""Curve interface; times in years and rates in continuous annual decimals."""
from abc import ABC, abstractmethod
from math import isfinite, log


class YieldCurve(ABC):
    @abstractmethod
    def discount_factor(self, maturity: float) -> float:
        """Present value of one unit paid at maturity; D(0) = 1."""

    def zero_rate(self, maturity: float) -> float:
        if not isfinite(maturity) or maturity <= 0:
            raise ValueError("zero-rate maturity must be finite and positive")
        return -log(self.discount_factor(maturity)) / maturity

    def forward_rate(self, start: float, end: float) -> float:
        if not isfinite(start) or not isfinite(end) or not 0 <= start < end:
            raise ValueError("forward interval must satisfy 0 <= start < end")
        return (log(self.discount_factor(start)) - log(self.discount_factor(end))) / (end - start)

