"""Arithmetic contribution and cost attribution with explicit reconciliation."""
from dataclasses import dataclass

import pandas as pd

from .metrics import validate_returns


@dataclass(frozen=True)
class AttributionReport:
    contributions: pd.DataFrame
    portfolio_total_return: float
    attributed_total_return: float
    residual: float
    reconciled: bool


def aggregate_attribution(contributions: pd.DataFrame, portfolio_returns, *,
                          group_column="group", contribution_column="contribution",
                          tolerance=1e-10) -> AttributionReport:
    required = {group_column, contribution_column}
    missing = required - set(contributions.columns)
    if missing:
        raise ValueError(f"attribution data missing columns: {', '.join(sorted(missing))}")
    frame = contributions.copy()
    frame[contribution_column] = pd.to_numeric(frame[contribution_column], errors="raise")
    if frame.empty or frame[group_column].isna().any() or frame[contribution_column].isna().any():
        raise ValueError("attribution data must be nonempty and complete")
    grouped = (frame.groupby(group_column, as_index=False)[contribution_column].sum()
               .rename(columns={contribution_column: "total_contribution"}))
    portfolio_total = float(validate_returns(portfolio_returns).sum())
    attributed_total = float(grouped["total_contribution"].sum())
    residual = portfolio_total - attributed_total
    return AttributionReport(grouped, portfolio_total, attributed_total, residual,
                             abs(residual) <= tolerance)


def gross_to_net_attribution(gross_returns, transaction_cost_returns,
                             financing_cost_returns=None, borrow_cost_returns=None) -> pd.DataFrame:
    gross = validate_returns(gross_returns, name="gross_returns").reset_index(drop=True)
    transaction = pd.Series(transaction_cost_returns, dtype=float).reset_index(drop=True)
    financing = pd.Series(0.0, index=gross.index) if financing_cost_returns is None else pd.Series(financing_cost_returns, dtype=float).reset_index(drop=True)
    borrow = pd.Series(0.0, index=gross.index) if borrow_cost_returns is None else pd.Series(borrow_cost_returns, dtype=float).reset_index(drop=True)
    if not (len(gross) == len(transaction) == len(financing) == len(borrow)):
        raise ValueError("gross returns and cost series must have equal length")
    if any(series.isna().any() or not series.map(lambda value: pd.notna(value) and abs(value) != float("inf")).all()
           or (series < 0).any() for series in (transaction, financing, borrow)):
        raise ValueError("cost returns must be complete and nonnegative")
    net = gross - transaction - financing - borrow
    return pd.DataFrame({"gross_return": gross, "transaction_cost": transaction,
                         "financing_cost": financing, "borrow_cost": borrow,
                         "net_return": net})
