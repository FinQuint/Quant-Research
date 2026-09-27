# Phase 10C push notes

## Target

- Repository: `FinQuint/Quant-Research`
- Base branch: `main`
- Feature branch: `feature/research-evaluation`
- Verified base commit: `ef59e3afdd24bc57786ebadbb85aea83fb032827`

Upload or copy the complete repository contents into a clone of
`FinQuint/Quant-Research` on the feature branch. Merge the new and changed files;
do not create another top-level `Quant-Research` directory. Preserve the existing
`.github/workflows/tests.yml` path so GitHub Actions detects the workflow.

## Suggested Git metadata

Commit message:

```text
feat: add adversarial quant research evaluation
```

Pull-request title:

```text
Phase 10C: Add adversarial quantitative research evaluation
```

Pull-request description:

```text
Adds a deterministic research-quality gate covering point-in-time integrity,
data quality, out-of-sample design, benchmark declaration, transaction costs,
multiple testing, and subperiod stability. The evaluation suite integrates with
both QuantPipeline and the Phase 10B approved-validation boundary. Includes
tests, policy configuration, documentation, a runnable example, and CI coverage.
```

## Recommended local sequence

```text
git switch main
git pull origin main
git switch -c feature/research-evaluation
python -m pip install -e ".[dev]"
pytest
python examples/adversarial_research_evaluation.py
git add .
git commit -m "feat: add adversarial quant research evaluation"
git push -u origin feature/research-evaluation
```

Open the pull request into `main` only after the Python 3.10, 3.11, and 3.12
GitHub Actions jobs pass. Human approval remains required for research promotion.
