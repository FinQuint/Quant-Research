from math import isfinite
from typing import Iterable

from finquint.data import QuantDataset
from finquint.pipeline import PipelineContext, PipelineStage

from ..bond import Bond
from ..curve_pricing import price_bond_with_curve
from ..curves import BondQuote, YieldCurve, bootstrap_bond_curve


def _dataset_quotes(data: QuantDataset) -> tuple[BondQuote, ...]:
    required = ("face_value", "coupon_rate", "maturity_years", "frequency", "market_price")
    missing = set(required) - set(data.data.columns)
    if missing:
        raise ValueError(f"missing curve quote columns: {', '.join(sorted(missing))}")
    if data.data.empty:
        raise ValueError("curve quote dataset is empty")
    if data.data.columns.duplicated().any():
        raise ValueError("curve quote columns must be unique")
    quotes = []
    for index, row in data.data.iterrows():
        try:
            values = {key: float(row[key]) for key in required}
            if any(not isfinite(v) for v in values.values()):
                raise ValueError("all quote fields must be finite")
            frequency = values["frequency"]
            if frequency != int(frequency) or frequency <= 0:
                raise ValueError("frequency must be a positive integer")
            quotes.append(BondQuote(Bond(
                values["face_value"], values["coupon_rate"], values["maturity_years"], int(frequency)
            ), values["market_price"]))
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError(f"invalid curve quote at row {index}: {exc}") from exc
    return tuple(quotes)


class BootstrapYieldCurveStage(PipelineStage):
    name = "bootstrap_yield_curve"

    def __init__(self, quotes: Iterable[BondQuote] | None = None):
        self.quotes = tuple(quotes) if quotes is not None else None

    def run(self, data, context: PipelineContext):
        if self.quotes is None:
            if not isinstance(data, QuantDataset):
                raise TypeError("provide quotes or a QuantDataset")
            quotes = _dataset_quotes(data)
        else:
            quotes = self.quotes
        curve = bootstrap_bond_curve(quotes)
        calibration = []
        for quote in sorted(quotes, key=lambda q: q.bond.maturity_years):
            price = price_bond_with_curve(quote.bond, curve)
            calibration.append({
                "maturity_years": quote.bond.maturity_years,
                "market_price": quote.market_price,
                "model_price": price,
                "price_error": price - quote.market_price,
            })
        context.set("yield_curve", curve)
        context.set("curve_calibration", calibration)
        return data


class CurveBondPricingStage(PipelineStage):
    name = "curve_bond_pricing"

    def __init__(self, bond: Bond, *, curve: YieldCurve | None = None):
        self.bond = bond
        self.curve = curve

    def run(self, data, context: PipelineContext):
        curve = self.curve if self.curve is not None else context.get("yield_curve")
        if not isinstance(curve, YieldCurve):
            raise ValueError("a YieldCurve is required; run BootstrapYieldCurveStage first")
        context.set("curve_bond_price", price_bond_with_curve(self.bond, curve))
        return data
