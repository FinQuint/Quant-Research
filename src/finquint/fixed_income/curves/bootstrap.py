"""Sequential calibration to regular fixed-rate bond dirty prices."""
from dataclasses import dataclass
from math import exp, fsum, isfinite, log
from typing import Iterable

from ..bond import Bond
from ..cashflows import cash_flows
from .discount_curve import DiscountCurve


@dataclass(frozen=True)
class BondQuote:
    bond: Bond
    market_price: float

    def __post_init__(self) -> None:
        if not isinstance(self.bond, Bond):
            raise TypeError("bond must be a Bond")
        if not isfinite(self.market_price) or self.market_price <= 0:
            raise ValueError("market_price must be finite and positive")
        b = self.bond
        if any(not isfinite(v) for v in (b.face_value, b.coupon_rate, b.maturity_years, b.frequency)):
            raise ValueError("bond inputs must be finite")
        if isinstance(b.frequency, bool) or b.frequency != int(b.frequency):
            raise ValueError("coupon frequency must be an integer")
        if b.periods < 1:
            raise ValueError("bond must have at least one payment")


def bootstrap_bond_curve(
    quotes: Iterable[BondQuote], *, price_tolerance: float = 1e-10,
    max_iterations: int = 200,
) -> DiscountCurve:
    """Bootstrap one pillar per distinct bond maturity, shortest first.

    Unknown intermediate coupon discounts use log-linear interpolation from
    the previous pillar to the terminal discount being solved. Already fitted
    pillars do not move. Prices are in face-value currency units, not percent.
    Tolerance is relative to max(1, market_price). No maturity extrapolation.
    """
    if not isfinite(price_tolerance) or price_tolerance <= 0:
        raise ValueError("price_tolerance must be finite and positive")
    if isinstance(max_iterations, bool) or not isinstance(max_iterations, int) or max_iterations < 1:
        raise ValueError("max_iterations must be a positive integer")
    items = list(quotes)
    if not items or any(not isinstance(q, BondQuote) for q in items):
        raise ValueError("provide a nonempty sequence of BondQuote objects")
    items.sort(key=lambda q: q.bond.maturity_years)
    times = [q.bond.maturity_years for q in items]
    if any(b - a <= 1e-12 for a, b in zip(times, times[1:])):
        raise ValueError("duplicate or indistinguishable maturities are not supported")
    pillars: list[float] = []
    discounts: list[float] = []
    for quote in items:
        end = quote.bond.maturity_years
        previous = DiscountCurve(tuple(pillars), tuple(discounts)) if pillars else None
        start = pillars[-1] if pillars else 0.0
        start_log = log(discounts[-1]) if discounts else 0.0
        known_pv = 0.0
        unknown = []
        for time, amount in cash_flows(quote.bond):
            if time <= start:
                known_pv += amount * previous.discount_factor(time)
            else:
                unknown.append(((time - start) / (end - start), amount))
        if quote.market_price <= known_pv:
            raise ValueError(f"price at maturity {end} is no greater than already-discounted coupons")

        def residual(terminal_log: float) -> float:
            return known_pv + fsum(
                amount * exp((1 - weight) * start_log + weight * terminal_log)
                for weight, amount in unknown
            ) - quote.market_price

        low, high = -600.0, 600.0
        if residual(low) > 0 or residual(high) < 0:
            raise ValueError(f"cannot bracket a representable discount factor at {end}")
        tolerance = price_tolerance * max(1.0, quote.market_price)
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
            raise RuntimeError(f"bootstrap did not converge at maturity {end}")
        pillars.append(end)
        discounts.append(exp(midpoint))
    return DiscountCurve(tuple(pillars), tuple(discounts))

