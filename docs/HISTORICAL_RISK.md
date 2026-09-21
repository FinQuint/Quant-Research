# Historical risk and stress-testing methodology

Phase 9 treats P&L as positive for gains and negative for losses. Reported VaR
and Expected Shortfall are nonnegative loss amounts.

Historical VaR is the requested linearly interpolated empirical loss quantile.
Historical Expected Shortfall is the average of the worst
`ceil((1-confidence)*n)` losses. Parametric VaR assumes normally distributed P&L
and uses sample standard deviation. Gains-only samples are floored at zero risk.

Historical level changes support absolute differences, simple relative changes,
and log changes. Rolling volatility uses sample standard deviation and an
explicit square-root annualization factor. Covariance uses the unbiased sample
denominator. Linear scenario P&L is the dot product of named factor exposures
and named factor shocks. This supports replaying historical rate changes when
the exposures are DV01/key-rate-DV01 values, as well as custom named stresses.

A VaR breach occurs when realized loss is strictly greater than the corresponding
VaR estimate. Backtests return the number and rate of breaches and their supplied
labels; they do not claim statistical model approval.

Current exclusions include nonlinear full revaluation in the generic historical
engine, filtered historical simulation, volatility scaling, Monte Carlo VaR,
liquidity horizons, overlapping-horizon corrections, Kupiec/Christoffersen tests,
regulatory capital rules, and data-source integration. Examples are synthetic
and are not investment advice.
