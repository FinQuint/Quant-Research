from datetime import date
from typing import Iterable

from finquint.data import QuantDataset
from finquint.fixed_income import DayCount
from finquint.fixed_income.curves import YieldCurve
from finquint.pipeline import PipelineContext, PipelineStage

from .portfolio import Portfolio, load_portfolio, portfolio_risk_report
from .scenarios import CurveScenario


class CurveRiskStage(PipelineStage):
    """Create named shocked curves for downstream inspection/repricing."""
    name = "curve_risk"

    def __init__(self, scenarios: Iterable[CurveScenario], *, curve: YieldCurve | None = None):
        self.scenarios = tuple(scenarios)
        if not self.scenarios or any(not isinstance(value, CurveScenario) for value in self.scenarios):
            raise ValueError("provide CurveScenario objects")
        names = [value.name for value in self.scenarios]
        if len(set(names)) != len(names):
            raise ValueError("scenario names must be unique")
        self.curve = curve

    def run(self, data, context: PipelineContext):
        curve = self.curve if self.curve is not None else context.get("yield_curve")
        if not isinstance(curve, YieldCurve):
            raise ValueError("a YieldCurve is required")
        context.set("curve_scenarios", {scenario.name: scenario.apply(curve) for scenario in self.scenarios})
        context.set("curve_scenario_definitions", [
            {"name": scenario.name, "node_shifts_bps": scenario.node_shifts_bps}
            for scenario in self.scenarios
        ])
        return data


class PortfolioRiskStage(PipelineStage):
    name = "portfolio_risk"

    def __init__(self, settlement: date, *, portfolio: Portfolio | None = None,
                 curve: YieldCurve | None = None, key_maturities=(2, 5, 10, 30),
                 scenarios: Iterable[CurveScenario] = (), bump_bps: float = 1,
                 curve_day_count: DayCount = DayCount.ACT_ACT_ISDA):
        self.settlement, self.portfolio, self.curve = settlement, portfolio, curve
        self.key_maturities, self.scenarios = tuple(key_maturities), tuple(scenarios)
        self.bump_bps, self.curve_day_count = bump_bps, curve_day_count

    def run(self, data, context: PipelineContext):
        portfolio = self.portfolio
        if portfolio is None:
            if not isinstance(data, QuantDataset):
                raise TypeError("provide portfolio or a QuantDataset")
            portfolio = load_portfolio(data)
        curve = self.curve if self.curve is not None else context.get("yield_curve")
        if not isinstance(curve, YieldCurve):
            raise ValueError("a YieldCurve is required")
        report = portfolio_risk_report(
            portfolio, self.settlement, curve, key_maturities=self.key_maturities,
            scenarios=self.scenarios, bump_bps=self.bump_bps,
            curve_day_count=self.curve_day_count,
        )
        context.set("portfolio", portfolio)
        context.set("portfolio_risk", report)
        return data
