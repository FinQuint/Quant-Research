"""Synthetic bond prices -> calibrated curve -> target bond price."""
from pathlib import Path

from finquint.data.connectors import CSVProvider
from finquint.data.stages import LoadDataStage, NormalizeDataStage, ValidateDataStage
from finquint.data.validation import Schema
from finquint.fixed_income import Bond
from finquint.fixed_income.stages import BootstrapYieldCurveStage, CurveBondPricingStage
from finquint.pipeline import QuantPipeline


def main():
    columns = ("face_value", "coupon_rate", "maturity_years", "frequency", "market_price")
    path = Path(__file__).parent / "data" / "curve_bonds.csv"
    result = (
        QuantPipeline("yield_curve_research")
        .add(LoadDataStage(CSVProvider(path)))
        .add(NormalizeDataStage())
        .add(ValidateDataStage(Schema(columns, columns)))
        .add(BootstrapYieldCurveStage())
        .add(CurveBondPricingStage(Bond(100, 0.045, 4, 2)))
        .run()
    )
    curve = result.context.get("yield_curve")
    print("Synthetic inputs only; continuous annual rates")
    print("Years  Discount factor  Zero rate")
    for time in curve.maturities:
        print(f"{time:5.1f}  {curve.discount_factor(time):15.9f}  {curve.zero_rate(time):.6%}")
    print(f"1y-to-2y forward rate: {curve.forward_rate(1, 2):.6%}")
    print(f"4y target bond price: {result.context.get('curve_bond_price'):.8f}")
    residual = max(abs(q["price_error"]) for q in result.context.get("curve_calibration"))
    print(f"Maximum calibration price error: {residual:.3e}")


if __name__ == "__main__":
    main()
