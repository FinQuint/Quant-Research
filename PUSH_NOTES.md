# Phase 10A push notes

- Repository: `FinQuint/Quant-Research`
- Branch: `feature/research-governance`
- PR base: `main`
- Baseline: merged Phase 9 commit `9dff55d2708e4c65776b5a31e20cc0eae4790846`

Suggested commit: `feat: add governed research workflow foundation`

Suggested PR title: `Phase 10A: Add reproducible research governance and approval workflows`

This complete repository archive includes the research workflow foundation while
preserving all quantitative pipeline functionality. Extract the ZIP and upload
the contents of its inner `Quant-Research` folder at the repository root.
Include `.github` and `.gitignore`; never replace `.git` or secrets.

Before merging, run `python -m pytest` and
`python examples/governed_research_workflow.py`, then wait for the Python
3.10–3.12 GitHub Actions matrix.
