from typing import Any

from finquint.pipeline import PipelineContext, PipelineStage

from ..bond import Bond
from ..risk import convexity, dv01, macaulay_duration, modified_duration


class BondRiskStage(PipelineStage):
    name = "bond_risk"

    def __init__(self, bond: Bond, *, annual_yield: float | None = None):
        self.bond = bond
        self.annual_yield = annual_yield

    def run(self, data: Any, context: PipelineContext) -> Any:
        ytm = self.annual_yield if self.annual_yield is not None else context.get("ytm")
        if ytm is None:
            raise ValueError("annual_yield is required or must be produced by BondPricingStage")
        context.set("macaulay_duration", macaulay_duration(self.bond, ytm))
        context.set("modified_duration", modified_duration(self.bond, ytm))
        context.set("dv01", dv01(self.bond, ytm))
        context.set("convexity", convexity(self.bond, ytm))
        return data

