# Phase 11 push notes

## Target

- Repository: `FinQuint/Quant-Research`
- Base branch: `main` after Phase 10C has been merged
- Feature branch: `feature/event-driven-backtesting`

Copy the contents of the ZIP's `Quant-Research` folder into the repository root.
Merge the files rather than creating a nested `Quant-Research/Quant-Research`
directory. Keep the workflow at `.github/workflows/tests.yml`.

## Git metadata

Commit message:

```text
feat: add event-driven strategy backtesting
```

Pull-request title:

```text
Phase 11: Add event-driven strategy backtesting
```

Pull-request description:

```text
Adds a deterministic daily-bar backtesting engine with next-open market-order
execution, signed position and cash accounting, fixed and proportional costs,
short-sale and cash controls, visible order rejections, turnover reporting, and
reusable pipeline integration. Includes single-asset and multi-asset examples,
accounting and look-ahead regression tests, documentation, and CI coverage.
```

## VS Code terminal sequence

If the Phase 11 files are already copied into your working folder, create the
feature branch before attempting to switch back to `main`:

```text
git switch -c feature/event-driven-backtesting
git status
git add .
git commit -m "feat: add event-driven strategy backtesting"
git push -u origin feature/event-driven-backtesting
```

If that branch already exists, use `git switch feature/event-driven-backtesting`
and then commit and push. Open a pull request into `main` only after all Python
3.10, 3.11, and 3.12 checks pass.
