# Phase 9 validation — 2026-09-20

Baseline: `FinQuint/Quant-Research` `main` at
`69f09e04d9b4bb260ea774ea1f76a0cfc351a2aa`.

The complete local suite passes on Windows with Python 3.12: **115 passed**.
All 105 Phase 1–8 tests remain passing and ten Phase 9 tests were added.

Coverage includes historical VaR interpolation, tail Expected Shortfall,
parametric VaR, P&L sign conventions, gains-only floors, difference/relative/log
changes, rolling volatility, covariance, factor exposure P&L, missing-factor
validation, named stress scenarios, breach dates and rates, dataset-driven risk
stages, explicit P&L inputs, and missing-column failures. The end-to-end example
also runs successfully.

Python 3.10/3.11 and Linux are covered by GitHub Actions. Methodology and scope
limitations are documented in `docs/HISTORICAL_RISK.md`.
