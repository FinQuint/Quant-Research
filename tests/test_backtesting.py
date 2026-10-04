import pandas as pd
import pytest

from finquint.backtesting import (
    BacktestConfig,
    EventDrivenBacktester,
    MarketOrder,
    validate_bars,
)


def bars(symbols=("AAA",), periods=4):
    dates = pd.date_range("2025-01-01", periods=periods, freq="D", tz="UTC")
    rows = []
    for date_index, timestamp in enumerate(dates):
        for symbol_index, symbol in enumerate(symbols):
            base = 100 + 10 * symbol_index + date_index
            rows.append({"timestamp": timestamp, "symbol": symbol,
                         "open": float(base), "close": float(base + 0.5)})
    return pd.DataFrame(rows)


class BuyOnce:
    def __init__(self, quantity=10):
        self.quantity = quantity
        self.sent = False

    def on_bar(self, context):
        if self.sent:
            return ()
        self.sent = True
        return (MarketOrder("buy-1", "AAA", self.quantity, context.timestamp),)


def test_signal_executes_at_next_open_without_lookahead():
    result = EventDrivenBacktester().run(bars(), BuyOnce())
    assert len(result.fills) == 1
    assert result.fills.iloc[0]["price"] == 101.0
    assert result.fills.iloc[0]["filled_at"] == pd.Timestamp("2025-01-02", tz="UTC")
    assert result.positions.iloc[-1]["quantity"] == 10


def test_cash_positions_and_equity_reconcile():
    result = EventDrivenBacktester(BacktestConfig(initial_cash=10_000)).run(bars(), BuyOnce())
    final = result.equity_curve.iloc[-1]
    position = result.positions.iloc[-1]
    assert final["equity"] == pytest.approx(final["cash"] + position["quantity"] * position["close"])


def test_transaction_costs_reduce_cash_and_equity():
    config = BacktestConfig(initial_cash=10_000, fixed_commission=2, proportional_cost_bps=10)
    result = EventDrivenBacktester(config).run(bars(), BuyOnce())
    expected_cost = 2 + 10 * 101 * 10 / 10_000
    assert result.total_transaction_costs == pytest.approx(expected_cost)
    assert result.cash_ledger.iloc[0]["cash"] == pytest.approx(10_000 - 1_010 - expected_cost)
    assert result.turnover > 0


def test_insufficient_cash_rejects_order():
    result = EventDrivenBacktester(BacktestConfig(initial_cash=100)).run(bars(), BuyOnce())
    assert result.fills.empty
    assert result.rejections.iloc[0]["reason"] == "insufficient cash"


def test_short_sale_policy_is_enforced():
    result = EventDrivenBacktester(BacktestConfig(allow_short=False)).run(bars(), BuyOnce(-10))
    assert result.fills.empty
    assert result.rejections.iloc[0]["reason"] == "short positions are disabled"


def test_order_on_final_bar_is_not_silently_dropped():
    class BuyLast:
        def on_bar(self, context):
            if len(context.history["timestamp"].drop_duplicates()) == 4:
                return (MarketOrder("late", "AAA", 1, context.timestamp),)
            return ()

    result = EventDrivenBacktester().run(bars(), BuyLast())
    assert result.rejections.iloc[0]["reason"] == "no later bar available for execution"


def test_market_data_must_be_unique_and_complete():
    duplicate = pd.concat([bars(), bars().iloc[[0]]], ignore_index=True)
    with pytest.raises(ValueError, match="unique"):
        validate_bars(duplicate)
    with pytest.raises(ValueError, match="missing columns"):
        validate_bars(pd.DataFrame({"timestamp": ["2025-01-01"]}))


def test_duplicate_order_ids_are_rejected():
    class Duplicate:
        def on_bar(self, context):
            return (MarketOrder("same", "AAA", 1, context.timestamp),)

    with pytest.raises(ValueError, match="duplicate order id"):
        EventDrivenBacktester().run(bars(), Duplicate())
