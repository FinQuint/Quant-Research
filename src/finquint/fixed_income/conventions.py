"""Explicit day-count conventions for dated fixed-income instruments."""
from calendar import isleap, monthrange
from datetime import date
from enum import Enum
from math import fsum


class DayCount(str, Enum):
    ACT_ACT_ISDA = "ACT/ACT ISDA"
    THIRTY_360_US = "30/360 US"
    THIRTY_E_360 = "30E/360"


def _check_dates(start: date, end: date) -> None:
    if not isinstance(start, date) or not isinstance(end, date):
        raise TypeError("start and end must be dates")


def _is_last_february_day(value: date) -> bool:
    return value.month == 2 and value.day == monthrange(value.year, 2)[1]


def year_fraction(start: date, end: date, convention: DayCount) -> float:
    """Return a signed year fraction; interval semantics are [start, end)."""
    _check_dates(start, end)
    try:
        convention = DayCount(convention)
    except ValueError as exc:
        raise ValueError(f"unsupported day-count convention: {convention!r}") from exc
    if start == end:
        return 0.0
    if end < start:
        return -year_fraction(end, start, convention)
    if convention is DayCount.ACT_ACT_ISDA:
        parts = []
        cursor = start
        while cursor < end:
            boundary = min(end, date(cursor.year + 1, 1, 1))
            parts.append((boundary - cursor).days / (366 if isleap(cursor.year) else 365))
            cursor = boundary
        return fsum(parts)
    d1, d2 = start.day, end.day
    if convention is DayCount.THIRTY_E_360:
        d1, d2 = min(d1, 30), min(d2, 30)
    else:
        start_eom_feb = _is_last_february_day(start)
        if start_eom_feb:
            d1 = 30
        if _is_last_february_day(end) and start_eom_feb:
            d2 = 30
        if d1 == 31:
            d1 = 30
        if d2 == 31 and d1 >= 30:
            d2 = 30
    return (360 * (end.year - start.year) + 30 * (end.month - start.month) + d2 - d1) / 360

