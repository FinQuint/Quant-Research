"""Generate datasets/demo_prices_v1.csv: synthetic daily bars for one symbol (DEMO), deterministic.

Synthetic data for the Phase 9 planner demo and evals; it is not market data. Trend regimes of
60-120 bars plus deterministic noise, so a momentum strategy has something to find in-sample and
something to lose out of sample. Re-running this script reproduces the file byte for byte (the
SHA-256 is pinned in configs/datasets.json).

    python datasets/make_demo_prices.py
"""
from math import sin
from pathlib import Path

import pandas as pd


def make(rows=640, seed=7):
    dates = pd.bdate_range("2022-01-03", periods=rows, tz="UTC")
    price, drift, out, regime_left = 100.0, 0.0006, [], 0
    for i, day in enumerate(dates):
        if regime_left == 0:
            regime_left = 60 + (i * 37 + seed) % 61
            drift = 0.0025 * sin(i * 0.61 + seed)
        regime_left -= 1
        noise = 0.008 * sin(i * 2.3 + seed) * sin(i * 0.37) + 0.004 * sin(i * 5.1)
        open_ = round(price * (1 + 0.25 * noise), 4)
        price = round(price * (1 + drift + noise), 4)
        out.append({"timestamp": day.strftime("%Y-%m-%d"), "symbol": "DEMO", "open": open_, "close": price})
    return pd.DataFrame(out)


if __name__ == "__main__":
    path = Path(__file__).with_name("demo_prices_v1.csv")
    make().to_csv(path, index=False, lineterminator="\n")
    print(path)
