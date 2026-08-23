from .bond import Bond
from .cashflows import cash_flows


def price_bond(bond: Bond, annual_yield: float) -> float:
    """Price a fixed-rate bond using nominal annual yield, compounded per coupon period."""
    periodic_yield = annual_yield / bond.frequency
    if periodic_yield <= -1:
        raise ValueError("annual_yield produces a non-positive discount base")
    return sum(
        amount / (1 + periodic_yield) ** (time * bond.frequency)
        for time, amount in cash_flows(bond)
    )

