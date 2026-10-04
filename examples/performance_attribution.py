"""Phase 12 performance, benchmark, cost, and group-attribution example."""
import pandas as pd

from finquint.performance import (
    PerformanceAnalysisStage,
    gross_to_net_attribution,
    subperiod_performance,
)
from finquint.pipeline import QuantPipeline


dates = pd.date_range("2025-01-01", periods=13, freq="D", tz="UTC")
equity = pd.DataFrame({
    "timestamp": dates,
    "equity": [100_000, 100_500, 100_200, 101_100, 101_600, 101_300,
               102_400, 102_100, 103_000, 103_700, 103_300, 104_200, 104_800],
})
returns = equity["equity"].pct_change().dropna().reset_index(drop=True)
benchmark = pd.Series([0.003, -0.002, 0.004, 0.002, -0.001, 0.006,
                       -0.002, 0.004, 0.005, -0.003, 0.004, 0.003])

# Arithmetic period contributions sum exactly to each portfolio return.
contributions = pd.DataFrame({
    "timestamp": list(dates[1:]) * 2,
    "group": ["rates"] * 12 + ["credit"] * 12,
    "contribution": list(returns * 0.60) + list(returns * 0.40),
})

result = QuantPipeline("performance_report").add(
    PerformanceAnalysisStage(benchmark_returns=benchmark,
                             contributions=contributions,
                             risk_free_rate=0.02,
                             periods_per_year=252,
                             rolling_window=5)
).run(equity)

summary = result.context.get("performance_summary")
attribution = result.context.get("performance_attribution")
cost_bridge = gross_to_net_attribution(
    returns + 0.0002, [0.00015] * len(returns), financing_cost_returns=[0.00005] * len(returns)
)
regimes = subperiod_performance(returns, ["first_half"] * 6 + ["second_half"] * 6)

print("Total return:", round(summary["total_return"], 6))
print("Sharpe ratio:", round(summary["sharpe_ratio"], 4))
print("Maximum drawdown:", round(summary["maximum_drawdown"], 6))
print("Attribution reconciled:", attribution.reconciled)
print("Gross-to-net reconciled:", (cost_bridge["gross_return"] - cost_bridge["transaction_cost"]
      - cost_bridge["financing_cost"] - cost_bridge["borrow_cost"]
      - cost_bridge["net_return"]).abs().max() < 1e-12)
print("Subperiods:", regimes["subperiod"].tolist())
