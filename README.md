# FinQuint Quant Research

A modular quantitative research framework. Fixed income is the first reference vertical, while pipeline, data, and analytics interfaces remain asset-class independent.

## Current scope
- Composable research pipeline
- Standard dataset/provider interfaces
- Numerical/statistical utilities
- Bond representation and cash flows
- Discounted-cash-flow bond pricing
- Tests and runnable example

## Install
`python -m pip install -e ".[dev]"`

## Test
`pytest`

## Roadmap
YTM -> duration/convexity/DV01 -> yield curves -> bootstrapping -> config-driven pipelines -> preprocessing integration -> term-structure models -> backtesting/risk.
