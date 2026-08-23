from typing import Any

from finquint.pipeline import PipelineContext, PipelineStage

from ..bond import Bond
from ..pricing import price_bond
from ..yield_metrics import yield_to_maturity


class BondPricingStage(PipelineStage):
    name = "bond_pricing"

    def __init__(self, bond: Bond, *, annual_yield: float | None = None, market_price: float | None = None):
        if annual_yield is None and market_price is None:
            raise ValueError("provide annual_yield or market_price")
        self.bond = bond
        self.annual_yield = annual_yield
        self.market_price = market_price

    def run(self, data: Any, context: PipelineContext) -> Any:
        ytm = self.annual_yield
        if ytm is None:
            ytm = yield_to_maturity(self.bond, self.market_price)
        price = self.market_price if self.market_price is not None else price_bond(self.bond, ytm)
        context.set("bond_price", price)
        context.set("ytm", ytm)
        return data

