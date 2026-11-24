"""Module sources the simulated coding agent proposes (CI and the Phase 10 eval).

MEAN_REVERSION is a clean template. The others each break one rule and name the layer that must
catch it: scope, static, conformance (lookahead, non-determinism, timeout) or the blind review.
"""

MEAN_REVERSION = '''"""Mean reversion on one symbol: fade large deviations of the close from its rolling mean."""
from finquint.backtesting import MarketOrder
from finquint.strategies.base import Param, StrategyTemplate


class MeanReversion:
    """z = (close[t] - mean of the last `window` closes) / their standard deviation.
    Target -units when z > entry_z, +units when z < -entry_z, flat when |z| < exit_z;
    unchanged otherwise or while there is not enough history. Orders fill at the next open."""

    def __init__(self, symbol, window=20, entry_z=1.5, exit_z=0.25, units=5000):
        self.symbol, self.window, self.entry_z, self.exit_z, self.units = symbol, window, entry_z, exit_z, units
        self._count = 0

    def on_bar(self, context):
        closes = context.history.loc[context.history["symbol"] == self.symbol, "close"]
        if len(closes) < self.window:
            return ()
        recent = closes.iloc[-self.window:].astype(float)
        spread = float(recent.std(ddof=0))
        if spread <= 0:
            return ()
        z = (float(recent.iloc[-1]) - float(recent.mean())) / spread
        current = context.positions.get(self.symbol, 0.0)
        if z > self.entry_z:
            target = -self.units
        elif z < -self.entry_z:
            target = self.units
        elif abs(z) < self.exit_z:
            target = 0.0
        else:
            return ()
        delta = target - current
        if abs(delta) < 1e-12:
            return ()
        self._count += 1
        return (MarketOrder(f"mr-{self._count}", self.symbol, delta, context.timestamp),)


TEMPLATE = StrategyTemplate(
    "mean_reversion",
    "Mean reversion on one symbol: short units when the close is more than entry_z rolling standard "
    "deviations above its window-bar mean, long when as far below, flat once within exit_z.",
    {"window": Param("integer", 5, 120, 20), "entry_z": Param("number", 0.5, 4.0, 1.5),
     "exit_z": Param("number", 0.0, 1.0, 0.25), "units": Param("number", 1, 10000, 5000)},
    MeanReversion)
'''


def _swap(old, new, source=MEAN_REVERSION):
    if old not in source:
        raise ValueError(f"example edit not found: {old!r}")
    return source.replace(old, new, 1)


# static: file access and a banned import
FILE_ACCESS = _swap("        self._count = 0\n",
                    "        self._count = 0\n        self._notes = open('notes.txt').read()\n")
OS_IMPORT = _swap("from finquint.backtesting import MarketOrder\n",
                  "import os\nfrom finquint.backtesting import MarketOrder\n")

# conformance: a class-level cache shared between runs lets a later run see bars it has not reached
LOOKAHEAD = _swap('''        self._count = 0

    def on_bar(self, context):
        closes = context.history.loc[context.history["symbol"] == self.symbol, "close"]
        if len(closes) < self.window:
            return ()''', '''        self._count = 0

    seen = {}

    def on_bar(self, context):
        closes = context.history.loc[context.history["symbol"] == self.symbol, "close"]
        self.seen[len(closes)] = float(closes.iloc[-1])
        ahead = self.seen.get(len(closes) + 1)
        if ahead is not None:
            target = self.units if ahead > float(closes.iloc[-1]) else -self.units
            delta = target - context.positions.get(self.symbol, 0.0)
            if abs(delta) < 1e-12:
                return ()
            self._count += 1
            return (MarketOrder(f"mr-{self._count}", self.symbol, delta, context.timestamp),)
        if len(closes) < self.window:
            return ()''')

# conformance: hidden state across runs flips the sign on every other run (not deterministic)
NONDETERMINISTIC = _swap('''        self._count = 0

    def on_bar''', '''        self._count = 0
        self.runs.append(1)
        self.sign = 1 if len(self.runs) % 2 else -1

    runs = []

    def on_bar''')
NONDETERMINISTIC = NONDETERMINISTIC.replace("target = -self.units", "target = -self.units * self.sign", 1)

# sandbox: never returns
INFINITE_LOOP = _swap('''        closes = context.history.loc''', '''        while self.units > 0:
            self._count += 1
        closes = context.history.loc''')

# blind review: a renamed copy of the time-series momentum template, offered for a mean-reversion card
MOMENTUM_COPY = '''"""Trend template on one symbol."""
from finquint.backtesting import MarketOrder
from finquint.strategies.base import Param, StrategyTemplate


class Trend:
    def __init__(self, symbol, lookback=60, units=5000):
        self.symbol, self.lookback, self.units = symbol, lookback, units
        self._count = 0

    def on_bar(self, context):
        closes = context.history.loc[context.history["symbol"] == self.symbol, "close"]
        if len(closes) <= self.lookback:
            return ()
        signal = float(closes.iloc[-1]) / float(closes.iloc[-1 - self.lookback]) - 1
        target = self.units if signal > 0 else -self.units
        delta = target - context.positions.get(self.symbol, 0.0)
        if abs(delta) < 1e-12:
            return ()
        self._count += 1
        return (MarketOrder(f"tr-{self._count}", self.symbol, delta, context.timestamp),)


TEMPLATE = StrategyTemplate(
    "reversion_fast",
    "Fast reversion on one symbol.",
    {"lookback": Param("integer", 5, 250, 60), "units": Param("number", 1, 10000, 5000)},
    Trend)
'''

__all__ = ["FILE_ACCESS", "INFINITE_LOOP", "LOOKAHEAD", "MEAN_REVERSION", "MOMENTUM_COPY", "NONDETERMINISTIC",
           "OS_IMPORT"]
