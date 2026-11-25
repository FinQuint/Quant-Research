import math

import numpy
import pandas

from finquint.backtesting import MarketOrder
from finquint.strategies.base import Param, StrategyTemplate


class MeanReversionStrategy:
    def __init__(self, symbol, lookback=20, entry_z=2.0, units=5000, long_only=False):
        self.symbol = symbol
        self.lookback = int(lookback)
        self.entry_z = float(entry_z)
        self.units = float(units)
        self.long_only = bool(long_only)

    def _closes(self, history):
        if history is None or len(history) == 0:
            return None
        rows = history.loc[history["symbol"] == self.symbol]
        if len(rows) == 0:
            return None
        rows = rows.sort_values("timestamp", kind="mergesort")
        closes = pandas.to_numeric(rows["close"], errors="coerce").dropna()
        return closes

    def on_bar(self, context):
        closes = self._closes(context.history)
        if closes is None or len(closes) < self.lookback or self.lookback < 2:
            return ()
        window = numpy.asarray(closes.iloc[-self.lookback:], dtype=float)
        mean = float(numpy.mean(window))
        std = float(numpy.std(window, ddof=1))
        last = float(window[-1])
        if not (math.isfinite(mean) and math.isfinite(std) and math.isfinite(last)):
            return ()
        if std <= 0.0:
            return ()
        z = (last - mean) / std
        positions = context.positions if context.positions is not None else {}
        current = float(positions.get(self.symbol, 0.0))
        target = current
        if z <= -self.entry_z:
            target = self.units
        elif z >= self.entry_z:
            target = 0.0 if self.long_only else -self.units
        elif current > 0.0 and z >= 0.0:
            target = 0.0
        elif current < 0.0 and z <= 0.0:
            target = 0.0
        delta = target - current
        if abs(delta) < 1e-9:
            return ()
        order_id = "mr-" + str(self.symbol) + "-" + str(context.timestamp)
        return (MarketOrder(order_id, self.symbol, delta, context.timestamp),)


TEMPLATE = StrategyTemplate(
    "mean_reversion",
    "Mean reversion on one symbol: compute the z-score of the latest close against the rolling mean and sample standard deviation of the last `lookback` closes; go long `units` when z <= -entry_z, short `units` (or flat when long_only) when z >= entry_z, and close any open position once the close crosses back through its rolling mean.",
    {
        "lookback": Param("integer", 5, 250, 20),
        "entry_z": Param("number", 0.5, 5.0, 2.0),
        "units": Param("number", 1, 10000, 5000),
        "long_only": Param("boolean", default=False),
    },
    MeanReversionStrategy,
)
