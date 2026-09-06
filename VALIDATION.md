# Phase 5 validation — 2026-09-06

Baseline: `FinQuint/Quant-Research` `main` at
`fe9bf02cd47ca171397a3af16e2d8578d60726ff`, fetched through GitHub.

Local environment: Windows, Python 3.12.14, pytest 9.1.1. Source was selected
explicitly from this Phase 5 tree; existing Phase 4 dependencies were reused.

- Full suite: **70 passed** in 1.32 seconds.
- Existing Phase 1–4 tests: **53 retained and passing**.
- New Phase 5 tests: **17 passing**.

Coverage includes multi-year ACT/ACT splitting, both 30/360 variants, signed
fractions, leap-year month-end schedules, explicit non-EOM schedules, stub
rejection, accrued interest, coupon-date legacy equivalence, clean/dirty round
trips, negative/zero/positive YTM round trips, duration/DV01/convexity repricing,
settlement bounds, solver validation, pipeline outputs, and compatibility.

The test run initially encountered a local temporary-directory permission error
in two inherited `tmp_path` tests; rerunning with a workspace-local pytest temp
directory produced the clean 70/70 result above. This was infrastructure-related,
not a test or model failure.

Python 3.10/3.11 and Linux were not executed locally. GitHub Actions will run
those after upload. These tests do not validate unsupported business calendars,
stub conventions, actual security documents, or production trading suitability.
