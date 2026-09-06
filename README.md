# FinQuint Quant Research

A modular quantitative research framework for standardized data ingestion, validation, normalization, and reusable analytics across asset classes. Fixed income is the first complete reference vertical.

## What is included

- Composable `QuantPipeline`, `PipelineStage`, and shared `PipelineContext`
- Stage timing, structured errors, and optional continue-on-error execution
- Standard `QuantDataset` contract
- CSV, JSON/JSONL, and Parquet providers
- Column normalization, dtype coercion, schema and null validation
- Fixed-rate bond cash flows and pricing
- Yield to maturity (dependency-free bracketed numerical solver)
- Macaulay duration, modified duration, DV01, and convexity
- Reusable bond pricing and risk pipeline stages
- Unit, integration, and end-to-end example coverage
- GitHub Actions test matrix for Python 3.10–3.12

## Installation

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
pytest
```

## Fixed-income functions

```python
from finquint.fixed_income import Bond, price_bond, yield_to_maturity

bond = Bond(face_value=100, coupon_rate=0.05, maturity_years=5, frequency=2)
price = price_bond(bond, annual_yield=0.04)
ytm = yield_to_maturity(bond, market_price=price)
```

Rates use decimals (`0.05` means 5%). Yield is a nominal annual rate compounded at the bond's coupon frequency. Maturities must resolve to a whole number of coupon periods. DV01 is the positive dollar price change for a one-basis-point fall in yield, calculated with a central difference.

## Pipeline example

```python
pipeline = (
    QuantPipeline(name="bond_analytics")
    .add(LoadDataStage(CSVProvider("bonds.csv")))
    .add(NormalizeDataStage())
    .add(ValidateDataStage(schema))
    .add(BondPricingStage(bond, market_price=100))
    .add(BondRiskStage(bond))
)
result = pipeline.run()
print(result.context.results)
```

Run the complete sample:

```bash
python examples/fixed_income_pipeline.py
```

The context contains `bond_price`, `ytm`, `macaulay_duration`, `modified_duration`, `dv01`, and `convexity`, while stage execution metadata is available in `context.metrics`.

## Repository structure

```text
src/finquint/
├── pipeline/               # Phase 1 execution engine
├── data/                   # Phase 2 dataset, providers, normalization, validation
└── fixed_income/           # Phase 3 bond analytics
    └── stages/             # reusable pricing and risk stages
tests/
examples/
.github/workflows/
```

## Phase 5: Dated bonds and settlement analytics

Phase 5 adds regular dated bonds, backward coupon schedule generation, explicit
`ACT/ACT ISDA`, `30/360 US`, and `30E/360` conventions, accrued interest,
clean/dirty conversion, settlement-aware YTM, duration, DV01, convexity, and a
reusable `DatedBondAnalyticsStage`. Existing Phase 1–4 APIs remain available.

```python
from datetime import date
from finquint.fixed_income import DatedBond, DayCount
from finquint.fixed_income.stages import DatedBondAnalyticsStage

bond = DatedBond(100, 0.05, date(2024, 1, 31), date(2029, 1, 31),
                 frequency=2, day_count=DayCount.THIRTY_360_US)
result = QuantPipeline().add(
    DatedBondAnalyticsStage(bond, date(2026, 4, 30), clean_price=101.25)
).run()
```

Run `python examples/dated_bond_analytics.py`. See
[market conventions](docs/MARKET_CONVENTIONS.md) for exact formulas and limits.
This first dated implementation supports only regular, unadjusted fixed-rate
bullet schedules; it explicitly rejects stubs and settlement at/after maturity.

## Roadmap

Phase 4 is implemented: see the section below. Next: dated instruments and
settlement conventions, deposit/swap calibration, curve risk, then term-structure
models and backtesting.

## Phase 4: Yield curves and bootstrapping

This release adds an immutable log-linear `DiscountCurve`, continuous zero and
forward rates, sequential calibration to bond prices, and curve-based pricing.
All Phase 3 APIs, including `generate_cashflows`, are preserved.

```python
from finquint.fixed_income import (
    Bond, BondQuote, bootstrap_bond_curve, price_bond_with_curve,
)

curve = bootstrap_bond_curve([
    BondQuote(Bond(100, 0.00, 0.5, 2), 98),
    BondQuote(Bond(100, 0.04, 1.0, 2), 99),
])
print(curve.discount_factor(0.75))
print(curve.zero_rate(1.0))
print(curve.forward_rate(0.5, 1.0))
print(price_bond_with_curve(Bond(100, 0.03, 1.0, 2), curve))
```

Run `python examples/yield_curve_pipeline.py` for the complete CSV-to-curve example.
`BootstrapYieldCurveStage()` consumes a `QuantDataset` with `face_value`,
`coupon_rate`, `maturity_years`, `frequency`, and `market_price` columns.
Alternatively, pass a list of `BondQuote` objects to its constructor.
It preserves the dataset and stores `yield_curve` and `curve_calibration` in
the context. `CurveBondPricingStage(bond)` stores `curve_bond_price`.

Curve zero/forward rates use continuous annual compounding; Phase 3 YTMs retain
nominal annual compounding at coupon frequency. Do not interchange them.
Times are years from a coupon date. Prices are dirty prices in face-value currency
units; accrued interest is zero under this regular coupon-date valuation model.
Negative rates are supported; extrapolation after the last pillar is rejected.
There must be one bond per maturity. Sparse maturities are supported.

Not included: settlement calendars, stubs, accrued-interest conversion, floating
rates, deposit/swap conventions, credit modeling, or multi-curve discounting.
Examples are synthetic research inputs, not investment advice.

See [conventions and algorithm](docs/YIELD_CURVES.md), [validation](VALIDATION.md),
and [upload instructions](PUSH_NOTES.md).

## License

MIT
