from datetime import date

import pytest

from finquint.fixed_income import DatedBond, DayCount, coupon_schedule, dated_cash_flows


def test_regular_schedule_and_cash_flows():
    bond = DatedBond(100, 0.05, date(2024, 1, 31), date(2026, 1, 31), 2)
    assert coupon_schedule(bond) == (
        date(2024, 1, 31), date(2024, 7, 31), date(2025, 1, 31),
        date(2025, 7, 31), date(2026, 1, 31),
    )
    assert [amount for _, amount in dated_cash_flows(bond)] == [2.5, 2.5, 2.5, 102.5]


def test_end_of_month_handles_leap_years():
    bond = DatedBond(100, 0.04, date(2023, 8, 31), date(2025, 2, 28), 2)
    assert coupon_schedule(bond) == (
        date(2023, 8, 31), date(2024, 2, 29), date(2024, 8, 31), date(2025, 2, 28)
    )


def test_non_eom_schedule_can_be_selected():
    bond = DatedBond(100, 0.04, date(2023, 8, 28), date(2025, 2, 28), 2, end_of_month=False)
    assert coupon_schedule(bond)[1] == date(2024, 2, 28)


def test_irregular_stub_and_bad_instruments_rejected():
    with pytest.raises(ValueError, match="irregular"):
        DatedBond(100, 0.05, date(2024, 2, 15), date(2025, 1, 31), 2)
    for kwargs in [
        {"face_value": 0}, {"coupon_rate": -0.01}, {"frequency": 5},
        {"issue_date": date(2025, 1, 1), "maturity_date": date(2025, 1, 1)},
    ]:
        values = dict(face_value=100, coupon_rate=0.05, issue_date=date(2024, 1, 1), maturity_date=date(2025, 1, 1), frequency=2)
        values.update(kwargs)
        with pytest.raises(ValueError):
            DatedBond(**values)
    with pytest.raises(ValueError):
        DatedBond(100, .05, date(2024, 1, 1), date(2025, 1, 1), 2, "ACT/365")

