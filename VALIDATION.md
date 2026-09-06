# Phase 6 validation — 2026-09-06

Baseline: `FinQuint/Quant-Research` `main` at
`fae763ffb81261c3248d029f3524388232550283`.

Local environment: Windows, Python 3.12.14. The full suite reports **89 passed**:
all 70 inherited Phase 1–5 tests and 19 new Phase 6 tests.

Phase 6 coverage includes settlement-aware curve pricing, clean/dirty conversion,
parallel shock equivalence, key-rate hat partitioning, named scenario
interpolation, signed positions, position-to-portfolio reconciliation, parallel
and key-rate DV01, effective duration and convexity, scenario P&L, CSV portfolio
loading, pipeline stage outputs, zero-market-value handling, and validation
errors. The runnable portfolio example was also executed successfully.

Python 3.10/3.11 and Linux are delegated to the included GitHub Actions matrix.
Unsupported conventions and production suitability are described in
`docs/PORTFOLIO_RISK.md`.
