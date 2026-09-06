# Dated bonds and market conventions

Phase 5 introduces `DatedBond` alongside the original year-based `Bond`.
The original API remains the compact research abstraction. The dated API is
explicit about contractual dates, settlement, price type, and day count.

## Instrument boundary

`DatedBond` represents a regular, bullet, fixed-rate bond with unadjusted
contractual coupon dates. `frequency` must be a positive divisor of 12.
Schedules are generated backward from maturity. Month-end maturity defaults to
month-end schedule generation; callers may explicitly set `end_of_month=False`.
The issue date must land on the regular schedule. Irregular first/last stubs are
rejected rather than approximated. There are no holiday calendars, ex-coupon
periods, payment delays, amortization, defaults, inflation linkage, or calls/puts.

## Day-count variants

- `ACT/ACT ISDA`: split `[start, end)` at calendar-year boundaries and divide
  each actual-day segment by 365 or 366 for that segment's year.
- `30/360 US`: apply the US/NASD end-of-month rules, including the February rule.
- `30E/360`: replace each day-of-month with `min(day, 30)`.

The exact variant is part of the instrument. The generic label “Actual/Actual”
or “30/360” is intentionally not accepted because those names are ambiguous.

## Settlement and price conventions

Settlement must be on/after issue and strictly before maturity. Cash flows on
the settlement date are not included; settlement on a coupon date begins a new
coupon period and accrued interest is zero.

For a regular coupon period:

```text
accrued interest = coupon payment × elapsed day-count fraction / full-period fraction
dirty price      = clean price + accrued interest
```

Prices are currency amounts for the bond's supplied face value, not percentage
quotes. YTM is a nominal annual decimal compounded at coupon frequency. The
first discount exponent is the remaining fraction of the coupon period, followed
by integer coupon-period increments. This is deliberately separate from Phase 4
continuous zero/forward curve rates.

`settlement_yield_to_maturity` solves dirty price by bracketed bisection.
Macaulay duration uses settlement-relative cash-flow times. Modified duration
uses Macaulay duration divided by `(1 + y/frequency)`. DV01 and convexity use
symmetric 1 bp repricing by default.

## Pipeline results

`DatedBondAnalyticsStage` accepts exactly one of `clean_price`, `dirty_price`, or
`annual_yield` and stores prefixed values so Phase 3 outputs are not overwritten:

- `dated_accrued_interest`, `dated_clean_price`, `dated_dirty_price`, `dated_ytm`
- `dated_macaulay_duration`, `dated_modified_duration`
- `dated_dv01`, `dated_convexity`

## Important exclusions

This phase is a deterministic research implementation, not a full security master
or trade-settlement engine. Match every instrument's documentation before using a
day-count convention. Clean/dirty terminology and common accrual bases vary by
bond type. Synthetic examples are not market data or investment advice.
