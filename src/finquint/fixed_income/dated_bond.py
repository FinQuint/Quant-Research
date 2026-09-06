from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from math import isfinite

from .conventions import DayCount


def _is_month_end(value: date) -> bool:
    return value.day == monthrange(value.year, value.month)[1]


def _shift_months(value: date, months: int, *, end_of_month: bool) -> date:
    index = value.year * 12 + value.month - 1 + months
    year, month0 = divmod(index, 12)
    month = month0 + 1
    last = monthrange(year, month)[1]
    return date(year, month, last if end_of_month else min(value.day, last))


@dataclass(frozen=True)
class DatedBond:
    """Regular fixed-rate bond; dates are contractual and unadjusted."""
    face_value: float
    coupon_rate: float
    issue_date: date
    maturity_date: date
    frequency: int = 2
    day_count: DayCount = DayCount.ACT_ACT_ISDA
    end_of_month: bool | None = None

    def __post_init__(self) -> None:
        if not isfinite(self.face_value) or self.face_value <= 0:
            raise ValueError("face_value must be finite and positive")
        if not isfinite(self.coupon_rate) or self.coupon_rate < 0:
            raise ValueError("coupon_rate must be finite and nonnegative")
        if not isinstance(self.issue_date, date) or not isinstance(self.maturity_date, date):
            raise TypeError("issue_date and maturity_date must be dates")
        if self.issue_date >= self.maturity_date:
            raise ValueError("issue_date must precede maturity_date")
        if isinstance(self.frequency, bool) or not isinstance(self.frequency, int) or self.frequency <= 0:
            raise ValueError("frequency must be a positive integer")
        if 12 % self.frequency:
            raise ValueError("frequency must divide 12")
        object.__setattr__(self, "day_count", DayCount(self.day_count))
        if self.end_of_month is None:
            object.__setattr__(self, "end_of_month", _is_month_end(self.maturity_date))
        elif not isinstance(self.end_of_month, bool):
            raise TypeError("end_of_month must be bool or None")
        coupon_schedule(self)  # Reject unsupported irregular first stubs immediately.

    @property
    def coupon_payment(self) -> float:
        return self.face_value * self.coupon_rate / self.frequency


def coupon_schedule(bond: DatedBond) -> tuple[date, ...]:
    """Accrual boundaries from issue through maturity, inclusive."""
    months = 12 // bond.frequency
    dates = [bond.maturity_date]
    cursor = bond.maturity_date
    while cursor > bond.issue_date:
        cursor = _shift_months(cursor, -months, end_of_month=bool(bond.end_of_month))
        dates.append(cursor)
    if dates[-1] != bond.issue_date:
        raise ValueError("irregular first coupon periods are not supported")
    return tuple(reversed(dates))


def dated_cash_flows(bond: DatedBond) -> tuple[tuple[date, float], ...]:
    schedule = coupon_schedule(bond)
    result = []
    for payment_date in schedule[1:]:
        amount = bond.coupon_payment
        if payment_date == bond.maturity_date:
            amount += bond.face_value
        result.append((payment_date, amount))
    return tuple(result)

