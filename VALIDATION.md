# Phase 8 validation — 2026-09-20

The complete local suite passes on Windows with Python 3.12: **105 passed**.
All 97 Phase 1–7 tests remain passing and eight Phase 8 tests were added.

Coverage includes Vasicek exact-transition reproducibility, CIR nonnegative
full-truncation paths, analytical zero-coupon boundary behavior, Feller reporting,
Vasicek and CIR parameter estimation from seeded synthetic observations, invalid
model inputs, dataset calibration, explicit-model simulation, pipeline context
outputs, and missing-input failures. The end-to-end example also runs cleanly.

Python 3.10/3.11 and Linux will run through the included GitHub Actions matrix.
Estimator and product limitations are documented in `docs/SHORT_RATE_MODELS.md`.
