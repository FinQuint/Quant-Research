# Phase 7 validation — 2026-09-06

The full local suite passes on Windows with Python 3.12: **97 passed**. This
retains all 89 Phase 1–6 tests and adds eight Phase 7 tests.

Coverage includes deposit discount factors, negative deposit rates, semiannual
par-swap repricing, duplicate and invalid quote rejection, separate discount and
projection behavior, projected simple forwards, CSV quote selection, reusable
pipeline stages, multi-curve assembly, and invalid input paths. The end-to-end
CSV example also runs successfully.

Python 3.10/3.11 and Linux are covered by the included GitHub Actions matrix once
uploaded. Scope limitations are recorded in `docs/MARKET_CURVES.md`.
