from .bond import Bond
from .cashflows import cash_flows
from .pricing import price_bond


def macaulay_duration(bond: Bond, annual_yield: float) -> float:
    price = price_bond(bond, annual_yield)
    periodic_yield = annual_yield / bond.frequency
    weighted = sum(
        time * amount / (1 + periodic_yield) ** (time * bond.frequency)
        for time, amount in cash_flows(bond)
    )
    return weighted / price


def modified_duration(bond: Bond, annual_yield: float) -> float:
    return macaulay_duration(bond, annual_yield) / (1 + annual_yield / bond.frequency)


def dv01(bond: Bond, annual_yield: float, *, bump: float = 0.0001) -> float:
    """Dollar value gained when yield falls one basis point (central difference)."""
    if bump <= 0:
        raise ValueError("bump must be positive")
    return (price_bond(bond, annual_yield - bump) - price_bond(bond, annual_yield + bump)) / 2


def convexity(bond: Bond, annual_yield: float) -> float:
    """Annualized discrete-compounding convexity: (1/P) d²P/dy²."""
    periodic_yield = annual_yield / bond.frequency
    price = price_bond(bond, annual_yield)
    total = 0.0
    for period, (_, amount) in enumerate(cash_flows(bond), start=1):
        total += amount * period * (period + 1) / (1 + periodic_yield) ** (period + 2)
    return total / (price * bond.frequency**2)

