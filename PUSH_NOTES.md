# Phase 6 push notes

- Repository: `FinQuint/Quant-Research`
- Branch: `feature/curve-portfolio-risk`
- PR base: `main`
- Baseline: merged Phase 5 commit `fae763ffb81261c3248d029f3524388232550283`

This ZIP is the complete repository, not a delta. Create the branch from current
`main`, extract the ZIP, and upload the **contents** of its inner
`Quant-Research` directory at the repository root. Include `.github` and
`.gitignore`; do not upload the ZIP itself or create a nested repository folder.
Preserve unrelated work added after the baseline and never replace `.git` or
secrets.

Suggested commit:

```text
feat: add curve scenarios and portfolio risk analytics
```

Suggested PR title:

```text
Phase 6: Add curve scenarios and portfolio risk analytics
```

Before merging, run `python -m pytest` and
`python examples/portfolio_risk_pipeline.py`, inspect the diff, and wait for the
Python 3.10–3.12 GitHub Actions matrix to pass.
