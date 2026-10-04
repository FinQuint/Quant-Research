from finquint.pipeline import PipelineContext, PipelineStage

from .attribution import aggregate_attribution
from .metrics import returns_from_equity, rolling_performance, summarize_performance


class PerformanceAnalysisStage(PipelineStage):
    name = "performance_analysis"

    def __init__(self, *, benchmark_returns=None, contributions=None, risk_free_rate=0.0,
                 periods_per_year=252, rolling_window=None):
        self.benchmark_returns = benchmark_returns
        self.contributions = contributions
        self.risk_free_rate = risk_free_rate
        self.periods_per_year = periods_per_year
        self.rolling_window = rolling_window

    def run(self, data, context: PipelineContext):
        backtest = context.get("backtest")
        source = backtest if backtest is not None else data
        if hasattr(source, "equity_curve"):
            equity = source.equity_curve["equity"]
        elif hasattr(source, "columns") and "equity" in source.columns:
            equity = source["equity"]
        else:
            raise ValueError("performance stage requires BacktestResult or an equity column")
        returns = returns_from_equity(equity)
        summary = summarize_performance(returns, benchmark_returns=self.benchmark_returns,
                                        risk_free_rate=self.risk_free_rate,
                                        periods_per_year=self.periods_per_year)
        context.set("performance_summary", summary.to_dict())
        context.set("performance_returns", returns)
        if self.rolling_window is not None:
            context.set("rolling_performance", rolling_performance(
                returns, self.rolling_window, risk_free_rate=self.risk_free_rate,
                periods_per_year=self.periods_per_year))
        if self.contributions is not None:
            context.set("performance_attribution", aggregate_attribution(
                self.contributions, returns))
        return data
