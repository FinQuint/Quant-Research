"""Phase 9 historical risk and stress-test example."""
from pathlib import Path

from finquint.data.connectors import CSVProvider
from finquint.data.stages import LoadDataStage, NormalizeDataStage
from finquint.pipeline import QuantPipeline
from finquint.risk import HistoricalRiskStage, StressScenario, StressTestStage, VaRBacktestStage

path = Path(__file__).parent / "data" / "historical_pnl.csv"
historical_pnl = (120, -85, 45, -210, 75, -40, 95, -330, 60, -125, 35, -70)
scenarios = (
    StressScenario("rates_up_100bp", {"five_year_dv01": 100}),
    StressScenario("rates_down_75bp", {"five_year_dv01": -75}),
)
result = (QuantPipeline("historical_risk")
          .add(LoadDataStage(CSVProvider(path)))
          .add(NormalizeDataStage())
          .add(HistoricalRiskStage(0.95))
          .add(StressTestStage({"five_year_dv01": -4.5}, scenarios))
          .add(VaRBacktestStage(historical_pnl, 200))
          .run())

print("Risk measures:", result.context.results["historical_risk"])
print("Stress tests:", result.context.results["stress_test"])
print("Backtest:", result.context.results["var_backtest"])
