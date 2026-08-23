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

## Roadmap

Phase 4 will add yield-curve construction and bootstrapping, followed by term-structure models, portfolio risk, and backtesting.

## License

MIT

