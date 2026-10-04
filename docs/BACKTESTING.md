# Event-driven backtesting

Phase 11 introduces a deterministic daily-bar engine. A strategy sees information
through the current close, submits market orders at that timestamp, and those
orders can execute only at the next available open. This timing rule is the core
look-ahead defense.

## Event sequence

For each timestamp, the engine performs these actions in order:

1. Execute previously submitted orders at the current open.
2. Update cash and positions and record fills or rejections.
3. Mark every position at the current close.
4. Record position, cash, cost, and equity snapshots.
5. Give the strategy current and historical bars through that close.
6. Queue newly submitted orders for a later bar.

An order placed on the final bar is explicitly rejected because no later bar is
available. Missing symbol bars delay execution rather than filling against stale
prices.

## Accounting conventions

For signed fill quantity `q`, execution price `p`, and cost `c`:

```text
cash change = -(q × p) - c
position change = q
equity = cash + Σ(position × latest close)
cost = fixed commission + |q × p| × cost_bps / 10,000
```

Positive quantities buy and negative quantities sell. Shorting and negative cash
are separate configuration choices. The engine rejects violations rather than
silently resizing orders. Turnover is absolute executed notional divided by
average recorded equity.

## Outputs

`BacktestResult` contains order, fill, rejection, position, cash-ledger, and
equity-curve tables plus aggregate transaction costs and turnover. `BacktestStage`
stores the complete result in `PipelineContext.results["backtest"]` and a compact
summary in `PipelineContext.results["backtest_summary"]`.

## Scope and limitations

This release intentionally supports daily bars and market orders only. It does
not model intraday sequencing, volume capacity, partial fills, bid/ask quotes,
corporate actions, financing, borrow availability, taxes, calendars, or live
broker behavior. Input prices must already reflect a documented adjustment
policy. These omissions must be addressed before interpreting results as
deployable performance.

Run:

```text
python examples/moving_average_backtest.py
python examples/scheduled_rebalance_backtest.py
```
