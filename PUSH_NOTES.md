# Phase 9 push notes

- Repository: `FinQuint/Quant-Research`
- Branch: `feature/historical-risk-stress-testing`
- PR base: `main`
- Baseline: merged Phase 8 commit `69f09e04d9b4bb260ea774ea1f76a0cfc351a2aa`

Suggested commit: `feat: add historical risk and stress testing`

Suggested PR title: `Phase 9: Add VaR, Expected Shortfall, and stress testing`

The complete repository ZIP is retained as a portable backup. When using it,
extract the archive and upload the contents of the inner `Quant-Research` folder
at the repository root. Include `.github` and `.gitignore`; never replace `.git`
or secrets.

Before merging, run `python -m pytest` and
`python examples/historical_risk_pipeline.py`, then wait for the Python
3.10–3.12 GitHub Actions matrix.
