"""Money-market and par-swap curve calibration."""
from dataclasses import dataclass
from math import exp, fsum, isfinite, log
from typing import Iterable

from .discount_curve import DiscountCurve


@dataclass(frozen=True)
class DepositQuote:
    maturity: float
    rate: float
    accrual: float | None = None

    def __post_init__(self):
        maturity = float(self.maturity)
        rate = float(self.rate)
        accrual = maturity if self.accrual is None else float(self.accrual)
        if not isfinite(maturity) or maturity <= 0:
            raise ValueError("deposit maturity must be finite and positive")
        if not isfinite(rate) or not isfinite(accrual) or accrual <= 0:
            raise ValueError("deposit rate and accrual must be valid")
        if 1 + rate * accrual <= 0:
            raise ValueError("deposit quote implies a nonpositive discount factor")
        object.__setattr__(self, "maturity", maturity)
        object.__setattr__(self, "rate", rate)
        object.__setattr__(self, "accrual", accrual)


@dataclass(frozen=True)
class SwapQuote:
    maturity: float
    fixed_rate: float
    frequency: int = 1

    def __post_init__(self):
        maturity, rate = float(self.maturity), float(self.fixed_rate)
        if not isfinite(maturity) or maturity <= 0 or not isfinite(rate):
            raise ValueError("swap maturity and rate must be finite")
        if isinstance(self.frequency, bool) or not isinstance(self.frequency, int) or self.frequency <= 0:
            raise ValueError("swap frequency must be a positive integer")
        periods = maturity * self.frequency
        if abs(periods - round(periods)) > 1e-10:
            raise ValueError("swap maturity must contain whole fixed-leg periods")
        object.__setattr__(self, "maturity", maturity)
        object.__setattr__(self, "fixed_rate", rate)


MarketQuote = DepositQuote | SwapQuote


def bootstrap_market_curve(quotes: Iterable[MarketQuote], *, tolerance: float = 1e-12,
                           max_iterations: int = 250) -> DiscountCurve:
    """Calibrate an OIS-style discount curve to simple deposits and par swaps."""
    items = sorted(tuple(quotes), key=lambda quote: quote.maturity)
    if not items or any(not isinstance(q, (DepositQuote, SwapQuote)) for q in items):
        raise ValueError("provide DepositQuote or SwapQuote objects")
    if any(b.maturity - a.maturity <= 1e-12 for a, b in zip(items, items[1:])):
        raise ValueError("quote maturities must be unique")
    if not isfinite(tolerance) or tolerance <= 0 or max_iterations < 1:
        raise ValueError("invalid solver controls")
    times: list[float] = []
    dfs: list[float] = []
    for quote in items:
        if isinstance(quote, DepositQuote):
            terminal = 1.0 / (1.0 + quote.rate * quote.accrual)
        else:
            end = quote.maturity
            start = times[-1] if times else 0.0
            start_log = log(dfs[-1]) if dfs else 0.0
            prior = DiscountCurve(tuple(times), tuple(dfs)) if times else None
            payment_times = tuple(i / quote.frequency for i in range(1, round(end * quote.frequency) + 1))
            accrual = 1.0 / quote.frequency

            def residual(terminal_log: float) -> float:
                values = []
                for payment in payment_times:
                    if prior is not None and payment <= start:
                        discount = prior.discount_factor(payment)
                    else:
                        weight = (payment - start) / (end - start)
                        discount = exp((1 - weight) * start_log + weight * terminal_log)
                    values.append(discount)
                return quote.fixed_rate * accrual * fsum(values) + values[-1] - 1.0

            low, high = -600.0, 600.0
            if residual(low) * residual(high) > 0:
                raise ValueError(f"cannot bracket swap pillar at {end}")
            for _ in range(max_iterations):
                midpoint = (low + high) / 2
                error = residual(midpoint)
                if abs(error) <= tolerance:
                    break
                if error > 0:
                    high = midpoint
                else:
                    low = midpoint
            else:
                raise RuntimeError(f"swap calibration did not converge at {end}")
            terminal = exp(midpoint)
        times.append(quote.maturity)
        dfs.append(terminal)
    return DiscountCurve(tuple(times), tuple(dfs))


def par_swap_rate(curve: DiscountCurve, maturity: float, frequency: int = 1) -> float:
    quote = SwapQuote(maturity, 0.0, frequency)
    payments = tuple(i / frequency for i in range(1, round(quote.maturity * frequency) + 1))
    annuity = fsum(curve.discount_factor(t) / frequency for t in payments)
    return (1.0 - curve.discount_factor(quote.maturity)) / annuity
