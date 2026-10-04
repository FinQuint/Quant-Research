import pandas as pd

from finquint.performance import PerformanceAnalysisStage
from finquint.pipeline import QuantPipeline


def test_performance_stage_analyzes_equity_and_publishes_rolling_results():
    equity = pd.DataFrame({"equity": [100, 101, 100.5, 102, 103, 102.5]})
    result = QuantPipeline("performance").add(
        PerformanceAnalysisStage(benchmark_returns=[0.005, -0.002, 0.004, 0.003, -0.001],
                                 periods_per_year=12, rolling_window=3)
    ).run(equity)
    summary = result.context.get("performance_summary")
    assert summary["observations"] == 5
    assert summary["tracking_error"] is not None
    assert len(result.context.get("rolling_performance")) == 3
    assert result.data.equals(equity)
