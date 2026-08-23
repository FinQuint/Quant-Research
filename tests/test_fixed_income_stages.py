import pytest

from finquint.fixed_income import Bond
from finquint.fixed_income.stages import BondPricingStage, BondRiskStage
from finquint.pipeline import QuantPipeline


def test_pricing_and_risk_stages_populate_context():
    bond = Bond(100, 0.05, 5, 2)
    result = (
        QuantPipeline("bond_analytics")
        .add(BondPricingStage(bond, market_price=100))
        .add(BondRiskStage(bond))
        .run()
    )
    assert result.context.results["bond_price"] == 100
    assert result.context.results["ytm"] == pytest.approx(0.05)
    assert set(result.context.results) == {
        "bond_price", "ytm", "macaulay_duration", "modified_duration", "dv01", "convexity"
    }

