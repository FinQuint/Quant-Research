"""End-to-end Phase 3 example. Run from the repository root."""
from pathlib import Path

from finquint.data.connectors import CSVProvider
from finquint.data.stages import LoadDataStage, NormalizeDataStage, ValidateDataStage
from finquint.data.validation import Schema
from finquint.fixed_income import Bond
from finquint.fixed_income.stages import BondPricingStage, BondRiskStage
from finquint.pipeline import QuantPipeline


data_path = Path(__file__).parent / "data" / "sample_bonds.csv"
provider = CSVProvider(data_path)
schema = Schema(
    required_columns=("face_value", "coupon_rate", "maturity_years", "frequency", "market_price"),
    non_null_columns=("face_value", "coupon_rate", "maturity_years", "frequency", "market_price"),
)
bond = Bond(face_value=100, coupon_rate=0.05, maturity_years=5, frequency=2)

result = (
    QuantPipeline(name="bond_analytics")
    .add(LoadDataStage(provider))
    .add(NormalizeDataStage())
    .add(ValidateDataStage(schema))
    .add(BondPricingStage(bond, market_price=100))
    .add(BondRiskStage(bond))
    .run()
)

for name, value in result.context.results.items():
    print(f"{name}: {value:.8f}")

