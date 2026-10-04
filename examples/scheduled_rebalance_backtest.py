"""Two-asset scheduled target-weight rebalance example."""
import pandas as pd

from finquint.backtesting import BacktestConfig, EventDrivenBacktester, MarketOrder


class ScheduledRebalance:
    def __init__(self, weights):
        self.weights = weights
        self.sequence = 0

    def on_bar(self, context):
        day = len(context.history["timestamp"].drop_duplicates())
        if day not in {1, 6}:
            return ()
        orders = []
        for symbol, weight in self.weights.items():
            desired = int(context.equity * weight / context.bars[symbol]["close"])
            quantity = desired - context.positions.get(symbol, 0)
            if quantity:
                self.sequence += 1
                orders.append(MarketOrder(f"rebalance-{self.sequence}", symbol, quantity, context.timestamp))
        return tuple(orders)


dates = pd.date_range("2025-01-01", periods=10, freq="D", tz="UTC")
rows = []
for index, timestamp in enumerate(dates):
    for symbol, start, drift in (("GOVT", 100, 0.2), ("CREDIT", 80, -0.1)):
        close = start + drift * index
        rows.append({"timestamp": timestamp, "symbol": symbol,
                     "open": close - 0.05, "close": close})

market = pd.DataFrame(rows)
result = EventDrivenBacktester(BacktestConfig(
    initial_cash=100_000, proportional_cost_bps=3, allow_negative_cash=True,
)).run(market, ScheduledRebalance({"GOVT": 0.60, "CREDIT": 0.40}))

print("Final equity:", round(result.equity_curve.iloc[-1]["equity"], 2))
print("Orders / fills / rejections:", len(result.orders), len(result.fills), len(result.rejections))
print("Final positions:")
print(result.positions[result.positions["timestamp"] == result.positions["timestamp"].max()][["symbol", "quantity"]])
