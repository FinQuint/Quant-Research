# Deposit, swap, and multi-curve conventions

Phase 7 calibrates log-linear discount-factor curves from simple money-market
deposits and fixed-for-floating par swaps. Rates are annual decimal rates.

A deposit with accrual `a` and rate `r` implies `D(T) = 1 / (1 + r*a)`.
For a par swap with fixed rate `K`, payment accrual `a`, and payment dates `t_i`,
calibration solves `K * sum(a*D(t_i)) + D(T) = 1`. Unknown discount factors
between the previous pillar and the swap maturity are log-linearly interpolated
while the terminal discount factor is solved. Quote maturities must be unique.

`MultiCurveSet` separates the curve used to discount cash flows from the curve
used to project floating rates. Its projected simple forward is
`(P(start)/P(end) - 1) / accrual`, using projection-curve pseudo-discounts.

This research implementation uses numeric year maturities and regular schedules.
It does not yet implement dated deposits/swaps, calendars, settlement lags,
payment delays, stubs, compounding indexes, futures convexity, OIS daily
compounding, cross-currency curves, collateral agreements, or simultaneous
global calibration. Examples are synthetic and are not investment advice.
