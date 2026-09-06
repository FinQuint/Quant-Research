from dataclasses import dataclass
from datetime import date
from math import fsum, isfinite
from typing import Iterable

from finquint.data import QuantDataset
from finquint.fixed_income import DatedBond, DayCount
from finquint.fixed_income.curve_settlement import dirty_price_with_curve
from finquint.fixed_income.curves import YieldCurve

from .scenarios import CurveScenario, key_rate_shift, parallel_shift


@dataclass(frozen=True)
class Position:
    identifier: str
    bond: DatedBond
    quantity: float

    def __post_init__(self):
        if not isinstance(self.identifier, str) or not self.identifier.strip():
            raise ValueError("position identifier must be nonempty")
        if not isinstance(self.bond, DatedBond):
            raise TypeError("position bond must be DatedBond")
        if not isfinite(self.quantity) or self.quantity == 0:
            raise ValueError("quantity must be finite and nonzero")


@dataclass(frozen=True)
class Portfolio:
    positions: tuple[Position, ...]

    def __init__(self, positions: Iterable[Position]):
        values = tuple(positions)
        if not values or any(not isinstance(value, Position) for value in values):
            raise ValueError("portfolio requires Position objects")
        identifiers = [value.identifier for value in values]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("position identifiers must be unique")
        object.__setattr__(self, "positions", values)


def load_portfolio(dataset: QuantDataset) -> Portfolio:
    required = ("identifier", "quantity", "face_value", "coupon_rate", "issue_date",
                "maturity_date", "frequency", "day_count")
    if not isinstance(dataset, QuantDataset):
        raise TypeError("dataset must be QuantDataset")
    missing = set(required) - set(dataset.data.columns)
    if missing:
        raise ValueError(f"missing portfolio columns: {', '.join(sorted(missing))}")
    positions = []
    for index, row in dataset.data.iterrows():
        try:
            frequency = float(row["frequency"])
            if not frequency.is_integer():
                raise ValueError("frequency must be an integer")
            positions.append(Position(
                str(row["identifier"]),
                DatedBond(float(row["face_value"]), float(row["coupon_rate"]),
                          date.fromisoformat(str(row["issue_date"])),
                          date.fromisoformat(str(row["maturity_date"])), int(frequency),
                          DayCount(str(row["day_count"]))),
                float(row["quantity"]),
            ))
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError(f"invalid portfolio row {index}: {exc}") from exc
    return Portfolio(positions)


def _value(position: Position, settlement: date, curve: YieldCurve, curve_day_count: DayCount) -> tuple[float, float]:
    price = dirty_price_with_curve(position.bond, settlement, curve, curve_day_count=curve_day_count)
    return price, price * position.quantity


def portfolio_risk_report(
    portfolio: Portfolio, settlement: date, curve: YieldCurve, *,
    key_maturities: Iterable[float] = (2, 5, 10, 30),
    scenarios: Iterable[CurveScenario] = (), bump_bps: float = 1,
    curve_day_count: DayCount = DayCount.ACT_ACT_ISDA,
) -> dict:
    if not isinstance(portfolio, Portfolio) or not isinstance(curve, YieldCurve):
        raise TypeError("portfolio and curve have invalid types")
    keys = tuple(float(value) for value in key_maturities)
    # Validate once, including membership/ordering behavior.
    for key in keys:
        key_rate_shift(curve, key, 0, keys)
    bump = float(bump_bps)
    if not isfinite(bump) or bump <= 0:
        raise ValueError("bump_bps must be finite and positive")
    scenario_values = tuple(scenarios)
    if any(not isinstance(value, CurveScenario) for value in scenario_values):
        raise TypeError("scenarios must contain CurveScenario objects")
    names = [value.name for value in scenario_values]
    if len(set(names)) != len(names):
        raise ValueError("scenario names must be unique")

    down_curve, up_curve = parallel_shift(curve, -bump), parallel_shift(curve, bump)
    positions, total, total_down, total_up = [], 0.0, 0.0, 0.0
    for position in portfolio.positions:
        price, market_value = _value(position, settlement, curve, curve_day_count)
        _, down = _value(position, settlement, down_curve, curve_day_count)
        _, up = _value(position, settlement, up_curve, curve_day_count)
        key_dv01 = {}
        for key in keys:
            _, key_down = _value(position, settlement, key_rate_shift(curve, key, -bump, keys), curve_day_count)
            _, key_up = _value(position, settlement, key_rate_shift(curve, key, bump, keys), curve_day_count)
            key_dv01[str(key)] = (key_down - key_up) / (2 * bump)
        positions.append({
            "identifier": position.identifier, "quantity": position.quantity,
            "unit_dirty_price": price, "market_value": market_value,
            "parallel_dv01": (down - up) / (2 * bump), "key_rate_dv01": key_dv01,
        })
        total, total_down, total_up = total + market_value, total_down + down, total_up + up
    parallel_dv01 = (total_down - total_up) / (2 * bump)
    bump_decimal = bump / 10_000
    effective_duration = None if total == 0 else (total_down - total_up) / (2 * total * bump_decimal)
    effective_convexity = None if total == 0 else (total_down - 2 * total + total_up) / (total * bump_decimal**2)
    key_totals = {str(key): fsum(row["key_rate_dv01"][str(key)] for row in positions) for key in keys}
    scenario_report = []
    for scenario in scenario_values:
        shocked = fsum(_value(p, settlement, scenario.apply(curve), curve_day_count)[1] for p in portfolio.positions)
        scenario_report.append({"name": scenario.name, "market_value": shocked, "pnl": shocked - total})
    return {
        "settlement": settlement.isoformat(), "base_market_value": total,
        "parallel_dv01": parallel_dv01, "effective_duration": effective_duration,
        "effective_convexity": effective_convexity, "key_rate_dv01": key_totals,
        "positions": positions, "scenarios": scenario_report,
    }
