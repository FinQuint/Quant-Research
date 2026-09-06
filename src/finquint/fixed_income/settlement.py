"""Settlement-aware accrued interest, price, YTM, and rate risk."""
from datetime import date
from math import fsum, isfinite

from .conventions import year_fraction
from .dated_bond import DatedBond, coupon_schedule, dated_cash_flows


def _period(bond: DatedBond, settlement: date) -> tuple[date, date, int]:
    if not isinstance(settlement, date):
        raise TypeError("settlement must be a date")
    if not bond.issue_date <= settlement < bond.maturity_date:
        raise ValueError("settlement must be on/after issue and before maturity")
    schedule = coupon_schedule(bond)
    for index, next_date in enumerate(schedule[1:], start=1):
        if settlement < next_date:
            return schedule[index - 1], next_date, index
    raise RuntimeError("could not locate settlement period")


def accrued_interest(bond: DatedBond, settlement: date) -> float:
    previous, following, _ = _period(bond, settlement)
    elapsed = year_fraction(previous, settlement, bond.day_count)
    full = year_fraction(previous, following, bond.day_count)
    return bond.coupon_payment * elapsed / full


def clean_to_dirty(bond: DatedBond, settlement: date, clean_price: float) -> float:
    if not isfinite(clean_price) or clean_price < 0:
        raise ValueError("clean_price must be finite and nonnegative")
    return clean_price + accrued_interest(bond, settlement)


def dirty_to_clean(bond: DatedBond, settlement: date, dirty_price: float) -> float:
    if not isfinite(dirty_price) or dirty_price <= 0:
        raise ValueError("dirty_price must be finite and positive")
    clean = dirty_price - accrued_interest(bond, settlement)
    if clean < 0:
        raise ValueError("dirty_price cannot be less than accrued interest")
    return clean


def _discounted_terms(bond: DatedBond, settlement: date, annual_yield: float):
    if not isfinite(annual_yield) or annual_yield / bond.frequency <= -1:
        raise ValueError("annual_yield must be finite and produce a positive discount base")
    previous, following, first_index = _period(bond, settlement)
    full = year_fraction(previous, following, bond.day_count)
    remaining = year_fraction(settlement, following, bond.day_count) / full
    base = 1 + annual_yield / bond.frequency
    flows = dated_cash_flows(bond)[first_index - 1:]
    return tuple(
        ((remaining + offset) / bond.frequency, amount / base ** (remaining + offset))
        for offset, (_, amount) in enumerate(flows)
    )


def dirty_price_from_yield(bond: DatedBond, settlement: date, annual_yield: float) -> float:
    return fsum(present_value for _, present_value in _discounted_terms(bond, settlement, annual_yield))


def clean_price_from_yield(bond: DatedBond, settlement: date, annual_yield: float) -> float:
    return dirty_to_clean(bond, settlement, dirty_price_from_yield(bond, settlement, annual_yield))


def settlement_yield_to_maturity(
    bond: DatedBond, settlement: date, dirty_price: float, *, tolerance: float = 1e-12,
    max_iterations: int = 200,
) -> float:
    if not isfinite(dirty_price) or dirty_price <= 0:
        raise ValueError("dirty_price must be finite and positive")
    if (not isfinite(tolerance) or tolerance <= 0 or isinstance(max_iterations, bool)
            or not isinstance(max_iterations, int) or max_iterations < 1):
        raise ValueError("tolerance and max_iterations must be positive")
    low, high = -bond.frequency + 1e-12, 1.0
    while dirty_price_from_yield(bond, settlement, high) > dirty_price and high < 1_000_000:
        high *= 2
    for _ in range(max_iterations):
        midpoint = (low + high) / 2
        error = dirty_price_from_yield(bond, settlement, midpoint) - dirty_price
        if abs(error) <= tolerance * max(1.0, dirty_price):
            return midpoint
        if error > 0:
            low = midpoint
        else:
            high = midpoint
    raise RuntimeError("settlement YTM solver did not converge")


def settlement_macaulay_duration(bond: DatedBond, settlement: date, annual_yield: float) -> float:
    terms = _discounted_terms(bond, settlement, annual_yield)
    price = fsum(value for _, value in terms)
    return fsum(time * value for time, value in terms) / price


def settlement_modified_duration(bond: DatedBond, settlement: date, annual_yield: float) -> float:
    return settlement_macaulay_duration(bond, settlement, annual_yield) / (1 + annual_yield / bond.frequency)


def settlement_dv01(bond: DatedBond, settlement: date, annual_yield: float, *, bump: float = 0.0001) -> float:
    if not isfinite(bump) or bump <= 0:
        raise ValueError("bump must be finite and positive")
    return (dirty_price_from_yield(bond, settlement, annual_yield - bump)
            - dirty_price_from_yield(bond, settlement, annual_yield + bump)) / 2


def settlement_convexity(bond: DatedBond, settlement: date, annual_yield: float, *, bump: float = 0.0001) -> float:
    if not isfinite(bump) or bump <= 0:
        raise ValueError("bump must be finite and positive")
    center = dirty_price_from_yield(bond, settlement, annual_yield)
    return (dirty_price_from_yield(bond, settlement, annual_yield - bump) - 2 * center
            + dirty_price_from_yield(bond, settlement, annual_yield + bump)) / (center * bump**2)
