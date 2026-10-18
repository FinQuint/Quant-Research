# Phase 12 validation report

## Autonomous workflow validation — 2026-10-18

Baseline: `d249516a9224064bd0fee88c206cbd07d0cc3091` (main). Validated on
Windows with Python 3.12, explicitly importing this feature checkout through
`PYTHONPATH=src` because an unrelated installed checkout exists on the host.

- Existing baseline tests before edits: **173 passed**.
- Complete suite after integration: **198 passed**, including **25 new tests**.
- All **15** example programs, including the new autonomous example: **passed**.
- The sandbox's default temporary directory was inaccessible for the standalone
  example; rerunning with a writable workspace temporary directory passed.
- Python 3.10 and 3.11 were not run locally. The existing GitHub Actions matrix
  continues to cover 3.10–3.12 and now runs the new autonomous example.
- No live LLM/provider, external action, production dataset, generated patch
  execution or authenticated human UI is exercised by these offline tests.

See [autonomous workflow design](docs/AUTONOMOUS_WORKFLOW.md) for trust boundaries,
recovery/retry semantics, extension contracts and covered failure modes.

## Previous release validation

Validated on 2026-10-04 using the complete Phase 11 repository as the baseline.

## Results

- Full test suite: **173 passed**
- Performance and attribution example: **passed**
- Python compilation for `src` and `examples`: **passed**
- CI workflow covers Python 3.10, 3.11, and 3.12

## Phase 12 regression coverage

- equity levels convert to periodic returns correctly
- annualized return and volatility match documented formulas
- drawdowns use compounded running peaks
- Sharpe, Sortino, Calmar, and confidence intervals are reported
- tracking error, information ratio, beta, and alpha require aligned benchmarks
- rolling and labeled subperiod reports preserve their boundaries
- group contributions reconcile or expose a nonzero residual
- gross-to-net returns reconcile transaction, financing, and borrow costs
- costs cannot be negative or silently use mismatched observations
- pipeline execution publishes summaries and rolling results without mutating data

The example is synthetic and produces unusually high annualized ratios because
it contains few observations. It validates software behavior, not investment
performance or statistical significance.
