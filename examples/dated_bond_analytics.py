"""Settlement-aware analytics for a synthetic regular fixed-rate bond."""
from datetime import date

from finquint.fixed_income import DatedBond, DayCount, coupon_schedule
from finquint.fixed_income.stages import DatedBondAnalyticsStage
from finquint.pipeline import QuantPipeline


def main():
    bond = DatedBond(
        face_value=100,
        coupon_rate=0.05,
        issue_date=date(2024, 1, 31),
        maturity_date=date(2029, 1, 31),
        frequency=2,
        day_count=DayCount.THIRTY_360_US,
    )
    settlement = date(2026, 4, 30)
    result = QuantPipeline("dated_bond_research").add(
        DatedBondAnalyticsStage(bond, settlement, clean_price=101.25)
    ).run()

    print("Synthetic inputs only")
    print("Coupon dates:", ", ".join(value.isoformat() for value in coupon_schedule(bond)[1:]))
    for key, value in result.context.results.items():
        print(f"{key}: {value:.10f}")


if __name__ == "__main__":
    main()
