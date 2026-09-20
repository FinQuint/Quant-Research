"""Phase 8 calibration, pricing, and simulation example."""
from finquint.models import (
    CalibrateShortRateStage, SimulateShortRateStage, VasicekModel,
)
from finquint.pipeline import QuantPipeline

source = VasicekModel(0.8, 0.04, 0.012)
observations = source.simulate(0.035, 2500, 1 / 252, seed=7)[0]
result = (QuantPipeline("short_rate_research")
          .add(CalibrateShortRateStage("vasicek", 1 / 252, observations=observations))
          .add(SimulateShortRateStage(0.035, 12, 1 / 12, paths=3, seed=42))
          .run())

model = result.context.results["short_rate_model"]
print("Calibrated Vasicek model:", model)
print(f"Five-year zero-coupon price: {model.zero_coupon_price(0.035, 5):.8f}")
print("First simulated path:", result.context.results["short_rate_paths"][0])
