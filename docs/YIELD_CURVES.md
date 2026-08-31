# Yield-curve conventions and calibration

`YieldCurve` exposes `discount_factor(t)`, `zero_rate(t)`, and
`forward_rate(start, end)`. `DiscountCurve(maturities, discount_factors)`
implements it. Inputs are copied to immutable tuples. Pillars must be strictly
increasing and positive; the origin D(0)=1 is implicit.
`DiscountCurve.from_zero_rates(maturities, rates)` accepts continuous zero rates.

For t > 0, z(t) = -log(D(t))/t. For 0 <= a < b,
f(a,b) = [log(D(a)) - log(D(b))]/(b-a). Zero rates at t=0 are rejected.
Interpolation is linear in log(D), including between the origin and first pillar,
implying a constant forward rate per segment. Extrapolation is not performed.
Negative rates are allowed: D may exceed 1.

The interpolation follows the standard log-linear discount construction in
[QuantLib's implementation](https://github.com/lballabio/QuantLib/blob/master/ql/termstructures/yield/discountcurve.hpp).
FinQuint is an independent implementation; QuantLib is not a dependency.

## Bootstrap algorithm

1. Validate regular bonds and finite positive dirty prices.
2. Sort by maturity, rejecting duplicates/near-duplicates within 1e-12 years.
3. Hold all previously fitted discount factors fixed.
4. Discount earlier coupons on the known curve. For coupons beyond its last
   pillar, interpolate log(D) between that pillar and the unknown terminal D.
5. Solve sum(CF_i * D(t_i)) = market_price by bisection in terminal log(D).
6. Append the pillar. Later pillars never alter already fitted segments.

Log(D) is bounded to [-600, 600]. The default limit is 200 iterations with
price tolerance 1e-10 * max(1, market_price). Failure to bracket or converge is
reported explicitly. Prices no greater than already-discounted coupons are
inconsistent with positive remaining discount factors and are rejected.
Nonnegative fixed cash flows make price increasing in terminal log(D), giving
a unique solution when bracketed.

`curve_calibration` reports market/model prices and residuals per maturity.
Exact calibration does not establish that quotes are economically sensible or
free from credit/liquidity effects. This is a single deterministic research curve.

## Scope and compatibility

Only regular fixed-rate and zero-coupon bonds are calibrated. No date calendars,
stubs, accrued interest, deposits, swaps, or multi-curve models are implemented.
Prices are currency amounts at the supplied face value, not percentage quotes.

`price_bond`, `generate_cashflows`, `cash_flows`, all Phase 3 risk/YTM functions,
and existing pricing/risk stages keep their previous signatures and behavior.
Use `price_bond_with_curve` explicitly. Curve zero rates are not bond YTMs and
must not be substituted into the legacy YTM risk functions.
