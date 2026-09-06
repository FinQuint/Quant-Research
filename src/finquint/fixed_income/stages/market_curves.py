from math import isfinite
from typing import Iterable

from finquint.data import QuantDataset
from finquint.pipeline import PipelineContext, PipelineStage

from ..curves import (
    DepositQuote, MultiCurveSet, SwapQuote, YieldCurve, bootstrap_market_curve,
)


def dataset_market_quotes(data: QuantDataset, *, curve_name: str | None = None):
    required = {"instrument_type", "maturity", "rate"}
    missing = required - set(data.data.columns)
    if missing:
        raise ValueError(f"missing market quote columns: {', '.join(sorted(missing))}")
    frame = data.data
    if curve_name is not None:
        if "curve_name" not in frame.columns:
            raise ValueError("curve_name column is required")
        frame = frame[frame["curve_name"] == curve_name]
    quotes = []
    for index, row in frame.iterrows():
        try:
            kind = str(row["instrument_type"]).strip().lower()
            maturity, rate = float(row["maturity"]), float(row["rate"])
            if not isfinite(maturity) or not isfinite(rate):
                raise ValueError("maturity and rate must be finite")
            if kind == "deposit":
                raw = row.get("accrual", maturity)
                accrual = maturity if raw != raw else float(raw)
                quotes.append(DepositQuote(maturity, rate, accrual))
            elif kind == "swap":
                raw = row.get("frequency", 1)
                frequency = 1 if raw != raw else int(raw)
                quotes.append(SwapQuote(maturity, rate, frequency))
            else:
                raise ValueError(f"unsupported instrument_type {kind!r}")
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError(f"invalid market quote at row {index}: {exc}") from exc
    if not quotes:
        raise ValueError("market quote selection is empty")
    return tuple(quotes)


class BootstrapMarketCurveStage(PipelineStage):
    name = "bootstrap_market_curve"

    def __init__(self, quotes: Iterable[DepositQuote | SwapQuote] | None = None, *,
                 curve_name: str | None = None, result_key: str = "yield_curve"):
        self.quotes = None if quotes is None else tuple(quotes)
        self.curve_name, self.result_key = curve_name, result_key

    def run(self, data, context: PipelineContext):
        if self.quotes is None:
            if not isinstance(data, QuantDataset):
                raise TypeError("provide quotes or a QuantDataset")
            quotes = dataset_market_quotes(data, curve_name=self.curve_name)
        else:
            quotes = self.quotes
        curve = bootstrap_market_curve(quotes)
        context.set(self.result_key, curve)
        context.set(f"{self.result_key}_calibration", [
            {"instrument_type": "deposit" if isinstance(q, DepositQuote) else "swap",
             "maturity": q.maturity, "market_rate": q.rate if isinstance(q, DepositQuote) else q.fixed_rate}
            for q in quotes
        ])
        return data


class BuildMultiCurveStage(PipelineStage):
    name = "build_multi_curve"

    def __init__(self, *, discount_key="discount_curve", projection_key="projection_curve",
                 result_key="multi_curve"):
        self.discount_key, self.projection_key, self.result_key = discount_key, projection_key, result_key

    def run(self, data, context: PipelineContext):
        discount, projection = context.get(self.discount_key), context.get(self.projection_key)
        if not isinstance(discount, YieldCurve) or not isinstance(projection, YieldCurve):
            raise ValueError("discount and projection curves are required")
        context.set(self.result_key, MultiCurveSet(discount, projection))
        return data
