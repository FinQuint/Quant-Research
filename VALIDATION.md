# Phase 12 validation report

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
