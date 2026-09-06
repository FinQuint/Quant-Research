from finquint.pipeline import PipelineContext, PipelineStage

from ..dated_bond import DatedBond
from ..settlement import (
    accrued_interest, clean_price_from_yield, clean_to_dirty, dirty_price_from_yield,
    dirty_to_clean, settlement_convexity, settlement_dv01,
    settlement_macaulay_duration, settlement_modified_duration,
    settlement_yield_to_maturity,
)


class DatedBondAnalyticsStage(PipelineStage):
    name = "dated_bond_analytics"

    def __init__(self, bond: DatedBond, settlement, *, clean_price=None, dirty_price=None, annual_yield=None):
        supplied = sum(value is not None for value in (clean_price, dirty_price, annual_yield))
        if supplied != 1:
            raise ValueError("provide exactly one of clean_price, dirty_price, or annual_yield")
        self.bond, self.settlement = bond, settlement
        self.clean_price, self.dirty_price, self.annual_yield = clean_price, dirty_price, annual_yield

    def run(self, data, context: PipelineContext):
        ai = accrued_interest(self.bond, self.settlement)
        if self.annual_yield is not None:
            ytm = self.annual_yield
            dirty = dirty_price_from_yield(self.bond, self.settlement, ytm)
            clean = clean_price_from_yield(self.bond, self.settlement, ytm)
        else:
            dirty = self.dirty_price if self.dirty_price is not None else clean_to_dirty(
                self.bond, self.settlement, self.clean_price)
            clean = self.clean_price if self.clean_price is not None else dirty_to_clean(
                self.bond, self.settlement, dirty)
            ytm = settlement_yield_to_maturity(self.bond, self.settlement, dirty)
        values = {
            "dated_accrued_interest": ai, "dated_clean_price": clean,
            "dated_dirty_price": dirty, "dated_ytm": ytm,
            "dated_macaulay_duration": settlement_macaulay_duration(self.bond, self.settlement, ytm),
            "dated_modified_duration": settlement_modified_duration(self.bond, self.settlement, ytm),
            "dated_dv01": settlement_dv01(self.bond, self.settlement, ytm),
            "dated_convexity": settlement_convexity(self.bond, self.settlement, ytm),
        }
        context.results.update(values)
        return data
