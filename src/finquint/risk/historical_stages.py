from finquint.data import QuantDataset
from finquint.pipeline import PipelineContext, PipelineStage

from .historical import backtest_var, expected_shortfall, historical_var, parametric_var, stress_test


class HistoricalRiskStage(PipelineStage):
    name = "historical_risk"

    def __init__(self, confidence=0.99, *, pnl=None, column="pnl"):
        self.confidence, self.pnl, self.column = confidence, pnl, column

    def run(self, data, context: PipelineContext):
        if self.pnl is None:
            if not isinstance(data, QuantDataset) or self.column not in data.data:
                raise ValueError(f"dataset column {self.column!r} is required")
            pnl = data.data[self.column]
        else:
            pnl = self.pnl
        report = {"confidence": self.confidence,
                  "historical_var": historical_var(pnl, self.confidence),
                  "expected_shortfall": expected_shortfall(pnl, self.confidence),
                  "parametric_var": parametric_var(pnl, self.confidence)}
        context.set("historical_risk", report)
        return data


class StressTestStage(PipelineStage):
    name = "stress_test"

    def __init__(self, exposures, scenarios):
        self.exposures, self.scenarios = exposures, scenarios

    def run(self, data, context: PipelineContext):
        context.set("stress_test", stress_test(self.exposures, self.scenarios))
        return data


class VaRBacktestStage(PipelineStage):
    name = "var_backtest"

    def __init__(self, actual_pnl, var, *, labels=None):
        self.actual_pnl, self.var, self.labels = actual_pnl, var, labels

    def run(self, data, context: PipelineContext):
        context.set("var_backtest", backtest_var(self.actual_pnl, self.var, labels=self.labels))
        return data
