"""Phase 7 CSV-to-discount/projection curve example."""
from pathlib import Path

from finquint.data.connectors import CSVProvider
from finquint.data.stages import LoadDataStage, NormalizeDataStage
from finquint.fixed_income.stages import BootstrapMarketCurveStage, BuildMultiCurveStage
from finquint.pipeline import QuantPipeline

path = Path(__file__).parent / "data" / "market_curve_quotes.csv"
result = (QuantPipeline("market_multi_curve")
          .add(LoadDataStage(CSVProvider(path)))
          .add(NormalizeDataStage())
          .add(BootstrapMarketCurveStage(curve_name="discount", result_key="discount_curve"))
          .add(BootstrapMarketCurveStage(curve_name="projection", result_key="projection_curve"))
          .add(BuildMultiCurveStage())
          .run())

curves = result.context.results["multi_curve"]
print(f"Two-year discount factor: {curves.discount_factor(2):.8f}")
print(f"1y x 2y projected simple forward: {curves.forward_rate(1, 2):.8f}")
