import pandas as pd
import pytest

from finquint.data import QuantDataset
from finquint.models import CalibrateShortRateStage, SimulateShortRateStage, VasicekModel
from finquint.pipeline import QuantPipeline


def test_calibration_and_simulation_pipeline():
    source = VasicekModel(0.9, 0.04, 0.01)
    observations = source.simulate(0.035, 2000, 1 / 252, seed=4)[0]
    data = QuantDataset(pd.DataFrame({"short_rate": observations}))
    result = (QuantPipeline("short_rate")
              .add(CalibrateShortRateStage("vasicek", 1 / 252))
              .add(SimulateShortRateStage(0.035, 24, 1 / 12, paths=5, seed=10))
              .run(data))
    assert len(result.context.results["short_rate_paths"]) == 5
    assert result.context.results["short_rate_simulation"]["seed"] == 10


def test_simulation_stage_accepts_explicit_model():
    result = QuantPipeline().add(SimulateShortRateStage(
        0.03, 3, 0.25, model=VasicekModel(1, 0.04, 0.01), seed=1,
    )).run()
    assert len(result.context.results["short_rate_paths"][0]) == 4


def test_stage_validation():
    with pytest.raises(ValueError, match="model must"):
        CalibrateShortRateStage("hull-white", 0.1)
    with pytest.raises(Exception, match="column"):
        QuantPipeline().add(CalibrateShortRateStage("vasicek", 0.1)).run(QuantDataset(pd.DataFrame({"x": [1]})))
    with pytest.raises(Exception, match="required"):
        QuantPipeline().add(SimulateShortRateStage(0.03, 2, 0.1)).run()
