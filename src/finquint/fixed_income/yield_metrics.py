from .bond import Bond
from .pricing import price_bond


def yield_to_maturity(
    bond: Bond,
    market_price: float,
    *,
    tolerance: float = 1e-12,
    max_iterations: int = 200,
) -> float:
    """Solve nominal annual YTM with a dependency-free bracketed bisection method."""
    if market_price <= 0:
        raise ValueError("market_price must be positive")
    lower = -bond.frequency + 1e-12
    upper = 1.0
    while price_bond(bond, upper) > market_price and upper < 1_000_000:
        upper *= 2
    if price_bond(bond, upper) > market_price:
        raise ValueError("could not bracket yield")
    for _ in range(max_iterations):
        midpoint = (lower + upper) / 2
        difference = price_bond(bond, midpoint) - market_price
        if abs(difference) <= tolerance or (upper - lower) <= tolerance:
            return midpoint
        if difference > 0:
            lower = midpoint
        else:
            upper = midpoint
    raise RuntimeError("YTM solver did not converge")

