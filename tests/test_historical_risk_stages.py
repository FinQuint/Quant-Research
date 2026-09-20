import pandas as pd
import pytest

from finquint.data import QuantDataset
from finquint.pipeline import QuantPipeline
from finquint.risk import (
    HistoricalRiskStage, StressScenario, StressTestStage, VaRBacktestStage,
)


def test_historical_risk_pipeline():
    data = QuantDataset(pd.DataFrame({"pnl": [-4, 2, -1, 3, -8, 1]}))
    result = (QuantPipeline("historical_risk")
              .add(HistoricalRiskStage(0.9))
              .add(StressTestStage({"rates": -1000}, (StressScenario("up", {"rates": 0.01}),)))
              .add(VaRBacktestStage((-2, -9, 1), 5, labels=("d1", "d2", "d3")))
              .run(data))
    assert result.context.results["historical_risk"]["historical_var"] > 0
    assert result.context.results["stress_test"][0]["pnl"] == -10
    assert result.context.results["var_backtest"]["breaches"] == ("d2",)


def test_historical_stage_accepts_explicit_pnl():
    result = QuantPipeline().add(HistoricalRiskStage(0.95, pnl=(-1, -2, 3))).run()
    assert "expected_shortfall" in result.context.results["historical_risk"]


def test_historical_stage_requires_column():
    with pytest.raises(Exception, match="column"):
        QuantPipeline().add(HistoricalRiskStage()).run(QuantDataset(pd.DataFrame({"x": [1, 2]})))
