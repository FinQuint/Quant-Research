# Phase 4 validation — 2026-08-30

Baseline: FinQuint/Quant-Research main at
50c65e0ce444d08784b15f46f1b917737ddb0a43, fetched using the GitHub connector.

Environment: Windows, Python 3.12.13, pandas 3.0.5, NumPy 2.5.2,
PyArrow 25.0.1, pytest 9.1.1. Editable package 0.4.0 was installed into an
isolated workspace virtual environment with declared development dependencies.

- `python -m pytest -o addopts=''`: **53 passed** (14 existing, 39 new).
- All three example scripts executed successfully.
- Synthetic 4-year target bond curve price: **98.29796463** per 100 face.
- Maximum sample calibration price error: **9.641e-09** currency units.

Tests cover exact knots, interpolation, continuous rate identities, flat-yield
legacy equivalence, sparse coupons, negative/zero rates, invalid inputs,
solver failure, immutability, extrapolation rejection, and CSV pipeline integration.

An optional coverage-instrumented run hit a NumPy import error in this runtime.
Standard pytest (the CI command) passes without special settings; no coverage
percentage is claimed. Python 3.10/3.11 and Linux were not executed locally.
The GitHub Actions matrix will test those after upload.

Research-model tests do not validate real-world settlement, accrued interest,
credit/liquidity assumptions, or production trading suitability.
