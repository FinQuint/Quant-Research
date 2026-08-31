# Phase 4 push notes

Repository: FinQuint/Quant-Research
Feature branch: feature/yield-curve-bootstrap
PR base: main

This is the complete repository, not only incremental files. It is based on
main commit 50c65e0ce444d08784b15f46f1b917737ddb0a43 (merged Phase 3).
All 42 source snapshot files are retained, with the updates listed below.
This delivery has not created a branch, commit, or PR on GitHub.

## Upload through GitHub

1. Open FinQuint/Quant-Research and select main.
2. Create feature/yield-curve-bootstrap from main (a branch, not a repository).
3. Extract the ZIP and open its inner Quant-Research folder.
4. Upload the CONTENTS at the repository root, including .github and .gitignore.
   Do not upload the ZIP or nest another Quant-Research folder.
5. Review overlaps before committing. Preserve unrelated/newer changes if main
   has advanced since the baseline above. Never replace .git or secrets.
6. Open a PR: base main, compare feature/yield-curve-bootstrap.
7. Wait for Python 3.10, 3.11, and 3.12 checks to pass before merging.

Commit: `feat: add yield curves and bond bootstrapping`

PR title: `Phase 4: Add yield curves, bootstrapping, and curve pricing stages`

PR summary:

- Add an immutable log-linear discount curve and continuous zero/forward rates.
- Bootstrap regular fixed-rate and zero-coupon bonds, including sparse maturities.
- Add curve pricing and reusable dataset-to-curve pipeline stages.
- Preserve all Phase 3 APIs and tests.
- Add calibration/validation tests, CSV example, and convention documentation.
- Run CI on pull requests and main pushes, avoiding duplicate feature-branch runs.

## Files to review

New: src/finquint/fixed_income/curves/, curve_pricing.py, stages/curves.py,
tests/test_curves.py, tests/test_bootstrap.py, tests/test_curve_stages.py,
examples/yield_curve_pipeline.py, examples/data/curve_bonds.csv,
docs/YIELD_CURVES.md, VALIDATION.md.

Updated: fixed-income and stage exports, package version (0.4.0), README,
PUSH_NOTES, and .github/workflows/tests.yml. Existing cash-flow, pricing, risk,
YTM, data and pipeline implementations and tests are preserved from main.

## Verify locally

```bash
python -m venv .venv
# Activate .venv before running the following.
python -m pip install -e ".[dev]"
python -m pytest
python examples/simple_bond_pricing.py
python examples/fixed_income_pipeline.py
python examples/yield_curve_pipeline.py
```

See VALIDATION.md for local results. Cross-version GitHub checks still need
to run after upload; local success is not a remote CI result.
