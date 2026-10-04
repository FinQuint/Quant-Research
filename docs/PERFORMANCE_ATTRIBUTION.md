# Performance measurement and attribution

Phase 12 turns an equity curve into a transparent, benchmark-aware research
report. Every annualization convention is explicit and contribution reports
expose their reconciliation residual.

## Return and risk conventions

The implementation uses simple periodic returns. Total and annualized return are:

```text
total return = product(1 + r[t]) - 1
annualized return = product(1 + r[t])^(periods_per_year / observations) - 1
```

Annualized volatility is sample standard deviation times the square root of
`periods_per_year`. Sharpe uses the periodic equivalent of the supplied annual
risk-free rate. Sortino uses the root-mean-square negative excess return over all
observations. Maximum drawdown is the largest peak-to-trough decline of compounded
wealth; Calmar divides annualized return by its magnitude.

The mean-return confidence interval is a normal approximation around the
arithmetic mean, annualized by multiplication. It is not a confidence interval
for compounded wealth and may be unreliable for autocorrelated, skewed, or
heavy-tailed returns.

## Benchmark-relative statistics

Tracking error is the annualized sample volatility of strategy-minus-benchmark
returns. Information ratio is annualized mean active return divided by periodic
active-return volatility. Beta is sample covariance divided by benchmark sample
variance. Alpha is periodic Jensen alpha multiplied by `periods_per_year`.
Strategy and benchmark inputs must have equal length and aligned frequency.

## Attribution and reconciliation

`aggregate_attribution` implements arithmetic contribution attribution. Group
contributions are summed through time and compared with the arithmetic sum of
portfolio returns:

```text
residual = sum(portfolio returns) - sum(all contributions)
```

The report never forces the residual to zero. `reconciled` is true only when the
absolute residual is within the declared tolerance. Arithmetic attribution is
appropriate for transparent period contribution checks but is not geometric
multi-period linking.

The gross-to-net bridge reports:

```text
net return = gross return - transaction costs - financing costs - borrow costs
```

All cost inputs are nonnegative return deductions.

## Pipeline use

`PerformanceAnalysisStage` accepts an equity table directly or consumes the
`BacktestResult` already stored by Phase 11. It publishes `performance_summary`,
`performance_returns`, optional `rolling_performance`, and optional
`performance_attribution` into `PipelineContext`.

Run `python examples/performance_attribution.py` for a complete report.

## Limits

Reported statistics are descriptive, not proof of predictability. Serial
correlation, selection bias, unstable regimes, nonlinear exposures, and multiple
testing can invalidate conventional ratios and confidence intervals. Phase 10C
evaluation and independent human review remain required. This software does not
provide investment advice.
