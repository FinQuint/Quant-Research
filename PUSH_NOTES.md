# Phase 12 push notes

## Target

- Repository: `FinQuint/Quant-Research`
- Base branch: `main` after Phase 11 has been merged
- Feature branch: `feature/performance-attribution`

Copy the contents of the ZIP's `Quant-Research` folder into the repository root.
Merge files without creating a nested repository directory. Preserve
`.github/workflows/tests.yml` so GitHub detects the test matrix.

## Git metadata

Commit message:

```text
feat: add performance measurement and attribution
```

Pull-request title:

```text
Phase 12: Add performance measurement and attribution
```

Pull-request description:

```text
Adds absolute, risk-adjusted, drawdown, benchmark-relative, rolling, and
subperiod performance analytics. Includes confidence intervals, arithmetic group
attribution with explicit residual reconciliation, gross-to-net cost bridges,
Phase 11 BacktestResult integration, documentation, examples, tests, and CI.
```

## VS Code terminal sequence

After copying the Phase 12 files into the working folder:

```text
git switch -c feature/performance-attribution
git status
git add .
git commit -m "feat: add performance measurement and attribution"
git push -u origin feature/performance-attribution
```

If the branch already exists, use `git switch feature/performance-attribution`.
Open the pull request into `main` after all Python 3.10–3.12 checks pass.
