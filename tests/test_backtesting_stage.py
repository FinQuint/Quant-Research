import pandas as pd

from finquint.backtesting import BacktestConfig, BacktestStage, EventDrivenBacktester, MarketOrder
from finquint.pipeline import QuantPipeline


class OneOrder:
    def __init__(self):
        self.sent = False

    def on_bar(self, context):
        if self.sent:
            return ()
        self.sent = True
        return (MarketOrder("order-1", "AAA", 2, context.timestamp),)


def test_backtest_stage_preserves_data_and_publishes_results():
    data = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=3, tz="UTC"),
        "symbol": ["AAA"] * 3,
        "open": [100.0, 101.0, 102.0],
        "close": [100.5, 101.5, 102.5],
    })
    stage = BacktestStage(
        OneOrder(), backtester=EventDrivenBacktester(BacktestConfig(initial_cash=10_000))
    )
    result = QuantPipeline("strategy_test").add(stage).run(data)
    assert result.data.equals(data)
    assert result.context.get("backtest_summary")["fills"] == 1
    assert result.context.get("backtest").fills.iloc[0]["price"] == 101.0
