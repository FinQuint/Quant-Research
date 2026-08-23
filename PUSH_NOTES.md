# Phase 3 push notes

## Destination

- Repository: `FinQuint/Quant-Research`
- Feature branch: `feature/fixed-income-analytics`
- Base branch for pull request: `main`

Do not create a separate Phase 3 repository and do not upload the ZIP itself as a single file. Extract it first and commit the repository contents.

## Safest merge method

If `feature/data-integration` has already been merged into `main`, create `feature/fixed-income-analytics` from the updated `main`, then copy the extracted files over the branch checkout. Keep any newer repository-specific files that are not represented in this archive, and review conflicts before committing.

If Phase 1–2 is still only on `feature/data-integration`, either merge that branch first or create `feature/fixed-income-analytics` from it. The ZIP is complete, so for an empty/new `Quant-Research` repository you can upload all extracted contents directly.

Do not replace the `.git` directory, repository secrets, branch protections, or GitHub settings. The archive intentionally contains no `.git` directory.

## Suggested Git metadata

Commit message:

```text
feat: add fixed-income pricing and risk analytics
```

Pull request title:

```text
Phase 3: Add fixed-income analytics and pipeline stages
```

Suggested PR summary:

```text
Completes Phase 3 of the FinQuint quantitative research framework by adding fixed-rate bond analytics and reusable pipeline stages. Includes bond pricing, numerical YTM, Macaulay and modified duration, DV01, convexity, end-to-end data-to-risk execution, tests, documentation, and CI coverage.
```

## Before opening the PR

```bash
python -m venv .venv
pip install -e ".[dev]"
pytest
python examples/fixed_income_pipeline.py
```

