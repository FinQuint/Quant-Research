from datetime import date

import pytest

from finquint.fixed_income import DayCount, year_fraction


def test_actual_actual_isda_splits_calendar_years():
    expected = 184 / 365 + 182 / 366
    assert year_fraction(date(2019, 7, 1), date(2020, 7, 1), DayCount.ACT_ACT_ISDA) == pytest.approx(expected)
    assert year_fraction(date(2020, 2, 28), date(2020, 3, 1), DayCount.ACT_ACT_ISDA) == pytest.approx(2 / 366)


def test_30_360_variants_are_explicit():
    assert year_fraction(date(2024, 1, 31), date(2024, 2, 29), DayCount.THIRTY_360_US) == pytest.approx(29 / 360)
    assert year_fraction(date(2024, 2, 29), date(2024, 3, 31), DayCount.THIRTY_360_US) == pytest.approx(30 / 360)
    assert year_fraction(date(2024, 2, 29), date(2024, 3, 31), DayCount.THIRTY_E_360) == pytest.approx(31 / 360)
    assert year_fraction(date(2024, 1, 31), date(2024, 2, 29), DayCount.THIRTY_E_360) == pytest.approx(29 / 360)


def test_signed_zero_and_invalid_day_counts():
    start, end = date(2024, 1, 1), date(2024, 7, 1)
    assert year_fraction(start, start, DayCount.ACT_ACT_ISDA) == 0
    assert year_fraction(end, start, DayCount.THIRTY_E_360) == -0.5
    with pytest.raises(TypeError):
        year_fraction("2024-01-01", end, DayCount.ACT_ACT_ISDA)
    with pytest.raises(ValueError):
        year_fraction(start, end, "ACT/365")

