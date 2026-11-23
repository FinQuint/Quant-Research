"""Time-series momentum: a strategy template for the backtester (finquint.backtesting.Strategy)."""
from finquint.backtesting import MarketOrder


class TimeSeriesMomentum:
    """Decides at each close from the history the engine passes in, so it cannot see later bars.

    signal = close[t] / close[t - lookback] - 1 on `symbol`; target = +units if signal > 0,
    -units (0 when long_only) if signal < 0, unchanged when there is not enough history yet.
    Orders move the position to the target; they fill at the next open (engine rule).
    """

    def __init__(self, symbol, lookback=60, units=100, long_only=False):
        self.symbol, self.lookback, self.units, self.long_only = symbol, lookback, units, long_only
        self._count = 0

    def on_bar(self, context):
        closes = context.history.loc[context.history["symbol"] == self.symbol, "close"]
        if len(closes) <= self.lookback:
            return ()
        signal = float(closes.iloc[-1]) / float(closes.iloc[-1 - self.lookback]) - 1
        if signal > 0:
            target = self.units
        elif signal < 0:
            target = 0 if self.long_only else -self.units
        else:
            return ()
        delta = target - context.positions.get(self.symbol, 0.0)
        if abs(delta) < 1e-12:
            return ()
        self._count += 1
        return (MarketOrder(f"tsm-{self._count}", self.symbol, delta, context.timestamp),)
