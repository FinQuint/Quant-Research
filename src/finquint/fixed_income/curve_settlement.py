"""Value dated bond cash flows on a settlement-relative discount curve."""
from datetime import date
from math import fsum, isfinite

from .conventions import DayCount, year_fraction
from .curves import YieldCurve
from .dated_bond import DatedBond, dated_cash_flows
from .settlement import accrued_interest


def dirty_price_with_curve(
    bond: DatedBond, settlement: date, curve: YieldCurve,
    *, curve_day_count: DayCount = DayCount.ACT_ACT_ISDA,
) -> float:
    if not isinstance(curve, YieldCurve):
        raise TypeError("curve must implement YieldCurve")
    # Validate settlement even for a zero-coupon bond.
    accrued_interest(bond, settlement)
    values = []
    for payment_date, amount in dated_cash_flows(bond):
        if payment_date > settlement:
            time = year_fraction(settlement, payment_date, curve_day_count)
            discount = curve.discount_factor(time)
            if not isfinite(discount) or discount <= 0:
                raise ValueError("curve returned a non-finite or non-positive discount factor")
            values.append(amount * discount)
    return fsum(values)


def clean_price_with_curve(
    bond: DatedBond, settlement: date, curve: YieldCurve,
    *, curve_day_count: DayCount = DayCount.ACT_ACT_ISDA,
) -> float:
    return dirty_price_with_curve(bond, settlement, curve, curve_day_count=curve_day_count) - accrued_interest(bond, settlement)

