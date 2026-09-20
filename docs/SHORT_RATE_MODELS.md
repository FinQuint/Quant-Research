# Short-rate model conventions

Phase 8 implements the one-factor Vasicek and Cox–Ingersoll–Ross (CIR) models.
Rates and volatility inputs are annual decimals and time is measured in years.

Vasicek follows `dr = k(theta-r)dt + sigma dW` and is simulated using its exact
Gaussian transition. Negative rates are possible by design. CIR follows
`dr = k(theta-r)dt + sigma*sqrt(r)dW`; simulation uses full-truncation Euler and
therefore returns nonnegative paths. Both models provide their standard affine
analytical zero-coupon bond price. CIR reports whether the Feller condition
`2*k*theta >= sigma^2` holds, but does not require it.

Vasicek calibration uses the exact discrete AR(1) relationship and ordinary
least squares. CIR calibration uses an Euler conditional-mean regression with a
state-scaled residual variance estimate. These estimators are intentionally
transparent research baselines, not production calibration engines.

All simulations use local seeded random generators for reproducibility and do
not change global random state. Paths include the initial rate, so a simulation
with `n` steps contains `n+1` observations.

Not included: Hull–White curve fitting, time-dependent parameters, multifactor
models, maximum-likelihood CIR estimation, Kalman filtering, market-price-of-risk
estimation, derivative pricing, or Monte Carlo confidence intervals. Synthetic
examples are not investment advice.
