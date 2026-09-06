# Phase 5 push notes

- Repository: `FinQuint/Quant-Research`
- Branch: `feature/dated-bonds-market-conventions`
- PR base: `main`
- Baseline: merged Phase 4 commit `fe9bf02cd47ca171397a3af16e2d8578d60726ff`

This is a complete repository archive, not a delta. No remote branch or PR was
created by this delivery.

## Upload

1. Create `feature/dated-bonds-market-conventions` from current `main`.
2. Extract the ZIP and open the inner `Quant-Research` directory.
3. Upload its **contents** at the repository root, including hidden `.github`
   and `.gitignore`; do not upload the ZIP or create a nested repository folder.
4. If `main` advanced beyond the baseline above, preserve newer unrelated work
   and resolve overlapping files deliberately. Never replace `.git` or secrets.
5. Run the checks, inspect the diff, and merge only after Python 3.10–3.12 pass.

Commit:

```text
feat: add dated bonds and settlement conventions
```

PR title:

```text
Phase 5: Add dated bonds and settlement-aware analytics
```

PR summary:

- Add regular dated fixed-rate bonds and backward coupon schedules.
- Add ACT/ACT ISDA, 30/360 US, and 30E/360 as explicit conventions.
- Add accrued interest and clean/dirty price conversion.
- Add settlement-aware price, YTM, duration, DV01, and convexity.
- Add a reusable dated-bond analytics pipeline stage.
- Preserve all Phase 1–4 behavior and tests.
- Add examples, scope documentation, and CI example execution.

## Review boundaries carefully

The new dated model supports regular unadjusted bullet schedules only. It rejects
stub periods. Business-day adjustment, calendars, settlement lags, ex-coupon
rules, accrued-on-default behavior, amortization, and floating coupons are not
implemented. Prices are currency amounts at supplied face, not percentage quotes.

## Verify

```bash
python -m venv .venv
# Activate the environment.
python -m pip install -e ".[dev]"
python -m pytest
python examples/simple_bond_pricing.py
python examples/fixed_income_pipeline.py
python examples/yield_curve_pipeline.py
python examples/dated_bond_analytics.py
```

See `VALIDATION.md` for the local result. Remote cross-version checks still need
to run after upload.
