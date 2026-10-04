from finquint.pipeline import PipelineContext, PipelineStage

from .engine import EventDrivenBacktester


class BacktestStage(PipelineStage):
    name = "event_driven_backtest"

    def __init__(self, strategy, *, backtester=None):
        self.strategy = strategy
        self.backtester = backtester or EventDrivenBacktester()

    def run(self, data, context: PipelineContext):
        frame = data.data if hasattr(data, "data") else data
        result = self.backtester.run(frame, self.strategy)
        context.set("backtest", result)
        context.set("backtest_summary", {
            "final_equity": float(result.equity_curve.iloc[-1]["equity"]),
            "transaction_costs": result.total_transaction_costs,
            "turnover": result.turnover,
            "orders": len(result.orders),
            "fills": len(result.fills),
            "rejections": len(result.rejections),
        })
        return data
