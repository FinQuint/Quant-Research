from datetime import date
from pathlib import Path

from finquint.data.connectors import CSVProvider
from finquint.data.stages import LoadDataStage, NormalizeDataStage
from finquint.fixed_income import DiscountCurve
from finquint.pipeline import QuantPipeline
from finquint.risk import CurveRiskStage, PortfolioRiskStage, flattener_scenario, steepener_scenario

curve = DiscountCurve.from_zero_rates((2.0, 5.0, 10.0), (0.032, 0.039, 0.044))
scenarios = (steepener_scenario(), flattener_scenario())
portfolio_path = Path(__file__).parent / "data" / "portfolio.csv"

result = (
    QuantPipeline(name="portfolio_curve_risk")
    .add(LoadDataStage(CSVProvider(portfolio_path)))
    .add(NormalizeDataStage())
    .add(CurveRiskStage(scenarios, curve=curve))
    .add(PortfolioRiskStage(
        date(2024, 1, 1), curve=curve,
        key_maturities=(2.0, 5.0, 10.0), scenarios=scenarios,
    ))
    .run()
)
report = result.context.results["portfolio_risk"]
print(f"Market value: {report['base_market_value']:.6f}")
print(f"Parallel DV01: {report['parallel_dv01']:.6f}")
print(f"Effective duration: {report['effective_duration']:.6f}")
print(f"Effective convexity: {report['effective_convexity']:.6f}")
print("Key-rate DV01:", report["key_rate_dv01"])
print("Scenarios:", report["scenarios"])
