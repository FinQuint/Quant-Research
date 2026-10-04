"""Daily-bar engine: decisions at close, market-order execution at next open."""
from dataclasses import asdict

import pandas as pd

from .core import (
    BacktestConfig,
    BacktestResult,
    Fill,
    MarketOrder,
    Rejection,
    StrategyContext,
    validate_bars,
)


class EventDrivenBacktester:
    def __init__(self, config: BacktestConfig | None = None):
        self.config = config or BacktestConfig()

    def run(self, market_data: pd.DataFrame, strategy) -> BacktestResult:
        data = validate_bars(market_data)
        cash = self.config.initial_cash
        positions: dict[str, float] = {}
        last_close: dict[str, float] = {}
        pending: list[MarketOrder] = []
        seen_order_ids: set[str] = set()
        orders, fills, rejections = [], [], []
        position_rows, cash_rows, equity_rows = [], [], []
        total_traded = 0.0
        total_costs = 0.0

        timestamps = list(data["timestamp"].drop_duplicates())
        for timestamp in timestamps:
            current = data[data["timestamp"] == timestamp]
            bars = {row.symbol: {"open": float(row.open), "close": float(row.close)}
                    for row in current.itertuples()}

            still_pending = []
            for order in pending:
                if order.symbol not in bars:
                    still_pending.append(order)
                    continue
                price = bars[order.symbol]["open"]
                cost = self.config.fixed_commission + abs(order.quantity * price) * self.config.proportional_cost_bps / 10_000
                resulting_position = positions.get(order.symbol, 0.0) + order.quantity
                resulting_cash = cash - order.quantity * price - cost
                reason = None
                if not self.config.allow_short and resulting_position < -1e-12:
                    reason = "short positions are disabled"
                elif not self.config.allow_negative_cash and resulting_cash < -1e-12:
                    reason = "insufficient cash"
                if reason:
                    rejections.append(Rejection(order.order_id, order.symbol, timestamp, reason))
                    continue
                cash = resulting_cash
                positions[order.symbol] = resulting_position
                fill = Fill(order.order_id, order.symbol, order.quantity, price, cost, timestamp)
                fills.append(fill)
                total_traded += abs(fill.notional)
                total_costs += cost
                cash_rows.append({"timestamp": timestamp, "order_id": order.order_id,
                                  "cash_change": -fill.notional - cost, "cash": cash})
            pending = still_pending

            for symbol, bar in bars.items():
                last_close[symbol] = bar["close"]
            equity = cash + sum(quantity * last_close[symbol]
                                for symbol, quantity in positions.items() if symbol in last_close)
            for symbol in sorted(set(last_close) | set(positions)):
                position_rows.append({"timestamp": timestamp, "symbol": symbol,
                                      "quantity": positions.get(symbol, 0.0),
                                      "close": last_close.get(symbol)})
            equity_rows.append({"timestamp": timestamp, "cash": cash, "equity": equity,
                                "transaction_costs_to_date": total_costs})

            history = data[data["timestamp"] <= timestamp].copy()
            context = StrategyContext(timestamp, bars, history, cash, dict(positions), equity)
            submitted = tuple(strategy.on_bar(context) or ())
            for order in submitted:
                if not isinstance(order, MarketOrder):
                    raise TypeError("strategy must return MarketOrder objects")
                if order.submitted_at != timestamp:
                    raise ValueError("order submitted_at must equal the current timestamp")
                if order.order_id in seen_order_ids:
                    raise ValueError(f"duplicate order id: {order.order_id}")
                seen_order_ids.add(order.order_id)
                orders.append(order)
                pending.append(order)

        for order in pending:
            rejections.append(Rejection(order.order_id, order.symbol, timestamps[-1],
                                        "no later bar available for execution"))

        order_frame = pd.DataFrame([asdict(value) for value in orders],
                                   columns=["order_id", "symbol", "quantity", "submitted_at"])
        fill_frame = pd.DataFrame([asdict(value) for value in fills],
                                  columns=["order_id", "symbol", "quantity", "price", "transaction_cost", "filled_at"])
        rejection_frame = pd.DataFrame([asdict(value) for value in rejections],
                                       columns=["order_id", "symbol", "rejected_at", "reason"])
        average_equity = sum(row["equity"] for row in equity_rows) / len(equity_rows)
        turnover = total_traded / average_equity if average_equity else 0.0
        return BacktestResult(order_frame, fill_frame, rejection_frame,
                              pd.DataFrame(position_rows), pd.DataFrame(cash_rows),
                              pd.DataFrame(equity_rows), total_costs, turnover)
