# Portfolio and curve risk conventions

Phase 6 values dated fixed-rate bonds from settlement using the Phase 4 discount
curve and Phase 5 dated cash flows. Quantities are signed: positive quantities
are long positions and negative quantities are hedges or shorts.

## Curve shocks

Curve shifts are continuous zero-rate shifts:

```text
D_shocked(t) = D_base(t) * exp(-shift(t) * t)
```

Shock inputs are in basis points; one basis point is `0.0001`. Parallel shocks
use a constant shift. Key-rate shocks use piecewise-linear hat functions across
the supplied key tenors; the hats partition unity, so summing equal key-rate
shocks reproduces a parallel shift. Named steepener, flattener, and twist
scenarios interpolate node shifts linearly and hold endpoint shifts flat.

## Risk measures

All measures use full repricing. Parallel DV01 is the positive gain for a
one-basis-point fall in rates. Key-rate DV01 uses the same convention for each
isolated key tenor. Effective duration and convexity are central-difference
portfolio measures normalized by original market value and are `None` when net
market value is zero. Scenario P&L is shocked value minus base value.

## Boundaries

The implementation inherits Phase 5's regular, unadjusted fixed-rate bullet
schedule restriction and Phase 4's no-extrapolation rule. It excludes credit
spread risk, option-adjusted measures, floating-rate instruments, calendars,
deposit/swap calibration, historical VaR, simulation, and multi-curve pricing.
Examples are synthetic research inputs, not investment advice.
