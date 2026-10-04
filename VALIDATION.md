# Phase 11 validation report

Validated on 2026-10-04 using the complete Phase 10C repository as the baseline.

## Results

- Full test suite: **156 passed**
- Moving-average backtest example: **passed**
- Scheduled multi-asset rebalance example: **passed**
- Python compilation for `src` and `examples`: **passed**
- CI workflow covers Python 3.10, 3.11, and 3.12

## Phase 11 regression coverage

- close-time decisions execute only at the next available open
- cash plus marked positions reconciles to portfolio equity
- fixed and proportional costs reduce cash and equity
- insufficient cash is rejected
- short sales are rejected when disabled
- final-bar orders are not silently discarded
- market bars must be complete and unique by timestamp and symbol
- duplicate order identifiers are rejected
- pipeline integration preserves input data and publishes full results

The examples use synthetic daily bars. Passing software tests does not establish
the profitability, capacity, or deployability of any investment strategy.
