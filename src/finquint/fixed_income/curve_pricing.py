from math import fsum, isfinite

from .bond import Bond
from .cashflows import cash_flows
from .curves import BondQuote, YieldCurve


def price_bond_with_curve(bond: Bond, curve: YieldCurve) -> float:
    """Discount each cash flow; preserve the separate Phase 3 YTM pricing API."""
    BondQuote(bond, 1.0)  # Validate the instrument without inventing a yield.
    terms = []
    for maturity, amount in cash_flows(bond):
        discount = curve.discount_factor(maturity)
        if not isfinite(discount) or discount <= 0:
            raise ValueError("curve must return finite positive discount factors")
        terms.append(amount * discount)
    result = fsum(terms)
    if not isfinite(result):
        raise ValueError("curve price is not finite")
    return result

