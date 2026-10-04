"""Single-asset daily moving-average backtest with next-open execution."""
import pandas as pd

from finquint.backtesting import BacktestConfig, BacktestStage, EventDrivenBacktester, MarketOrder
from finquint.pipeline import QuantPipeline


class MovingAverageStrategy:
    def __init__(self, short_window=3, long_window=5, position_size=100):
        self.short_window = short_window
        self.long_window = long_window
        self.position_size = position_size
        self.sequence = 0

    def on_bar(self, context):
        closes = context.history.loc[context.history["symbol"] == "BOND_ETF", "close"]
        if len(closes) < self.long_window:
            return ()
        desired = self.position_size if closes.tail(self.short_window).mean() > closes.tail(self.long_window).mean() else 0
        quantity = desired - context.positions.get("BOND_ETF", 0)
        if quantity == 0:
            return ()
        self.sequence += 1
        return (MarketOrder(f"ma-{self.sequence}", "BOND_ETF", quantity, context.timestamp),)


dates = pd.date_range("2025-01-01", periods=12, freq="D", tz="UTC")
closes = [100, 99, 98, 99, 101, 103, 104, 102, 100, 98, 99, 101]
market = pd.DataFrame({
    "timestamp": dates,
    "symbol": "BOND_ETF",
    "open": [value - 0.2 for value in closes],
    "close": closes,
})

backtester = EventDrivenBacktester(BacktestConfig(
    initial_cash=100_000, fixed_commission=1.0, proportional_cost_bps=2.0,
))
result = QuantPipeline("moving_average").add(
    BacktestStage(MovingAverageStrategy(), backtester=backtester)
).run(market)

summary = result.context.get("backtest_summary")
print("Final equity:", round(summary["final_equity"], 2))
print("Fills:", summary["fills"])
print("Transaction costs:", round(summary["transaction_costs"], 2))
print("Turnover:", round(summary["turnover"], 6))
