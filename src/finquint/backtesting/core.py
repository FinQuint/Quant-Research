"""Deterministic daily-bar event-driven backtesting primitives."""
from dataclasses import asdict, dataclass
from enum import Enum
from math import isfinite
from typing import Mapping, Protocol, Sequence

import pandas as pd


class OrderStatus(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    REJECTED = "rejected"


@dataclass(frozen=True)
class MarketOrder:
    order_id: str
    symbol: str
    quantity: float
    submitted_at: pd.Timestamp

    def __post_init__(self):
        if not self.order_id.strip() or not self.symbol.strip():
            raise ValueError("order id and symbol are required")
        if not isfinite(self.quantity) or self.quantity == 0:
            raise ValueError("order quantity must be finite and nonzero")


@dataclass(frozen=True)
class Fill:
    order_id: str
    symbol: str
    quantity: float
    price: float
    transaction_cost: float
    filled_at: pd.Timestamp

    @property
    def notional(self) -> float:
        return self.quantity * self.price


@dataclass(frozen=True)
class Rejection:
    order_id: str
    symbol: str
    rejected_at: pd.Timestamp
    reason: str


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float = 1_000_000.0
    fixed_commission: float = 0.0
    proportional_cost_bps: float = 0.0
    allow_short: bool = True
    allow_negative_cash: bool = False

    def __post_init__(self):
        numeric = (self.initial_cash, self.fixed_commission, self.proportional_cost_bps)
        if any(not isfinite(value) for value in numeric):
            raise ValueError("backtest configuration must be finite")
        if self.initial_cash <= 0 or self.fixed_commission < 0 or self.proportional_cost_bps < 0:
            raise ValueError("cash must be positive and costs nonnegative")


@dataclass(frozen=True)
class StrategyContext:
    timestamp: pd.Timestamp
    bars: Mapping[str, Mapping[str, float]]
    history: pd.DataFrame
    cash: float
    positions: Mapping[str, float]
    equity: float


class Strategy(Protocol):
    def on_bar(self, context: StrategyContext) -> Sequence[MarketOrder]: ...


@dataclass(frozen=True)
class BacktestResult:
    orders: pd.DataFrame
    fills: pd.DataFrame
    rejections: pd.DataFrame
    positions: pd.DataFrame
    cash_ledger: pd.DataFrame
    equity_curve: pd.DataFrame
    total_transaction_costs: float
    turnover: float

    def to_dict(self) -> dict:
        return {
            "orders": self.orders.to_dict("records"),
            "fills": self.fills.to_dict("records"),
            "rejections": self.rejections.to_dict("records"),
            "positions": self.positions.to_dict("records"),
            "cash_ledger": self.cash_ledger.to_dict("records"),
            "equity_curve": self.equity_curve.to_dict("records"),
            "total_transaction_costs": self.total_transaction_costs,
            "turnover": self.turnover,
        }


def validate_bars(data: pd.DataFrame) -> pd.DataFrame:
    required = {"timestamp", "symbol", "open", "close"}
    missing = required - set(data.columns)
    if missing:
        raise ValueError(f"market data missing columns: {', '.join(sorted(missing))}")
    frame = data.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="raise")
    if frame.empty or frame.duplicated(["timestamp", "symbol"]).any():
        raise ValueError("market data must be nonempty and unique by timestamp and symbol")
    if frame[["open", "close"]].isna().any().any():
        raise ValueError("open and close prices cannot be missing")
    if (frame[["open", "close"]] <= 0).any().any():
        raise ValueError("open and close prices must be positive")
    return frame.sort_values(["timestamp", "symbol"]).reset_index(drop=True)
