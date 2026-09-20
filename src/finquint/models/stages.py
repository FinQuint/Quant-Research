from finquint.data import QuantDataset
from finquint.pipeline import PipelineContext, PipelineStage

from .short_rate import CIRModel, VasicekModel, calibrate_cir, calibrate_vasicek


class CalibrateShortRateStage(PipelineStage):
    name = "calibrate_short_rate"

    def __init__(self, model: str, dt: float, *, observations=None, column="short_rate"):
        self.model, self.dt, self.observations, self.column = model.lower(), dt, observations, column
        if self.model not in ("vasicek", "cir"):
            raise ValueError("model must be 'vasicek' or 'cir'")

    def run(self, data, context: PipelineContext):
        if self.observations is None:
            if not isinstance(data, QuantDataset) or self.column not in data.data:
                raise ValueError(f"dataset column {self.column!r} is required")
            observations = data.data[self.column]
        else:
            observations = self.observations
        calibrator = calibrate_vasicek if self.model == "vasicek" else calibrate_cir
        context.set("short_rate_model", calibrator(observations, self.dt))
        return data


class SimulateShortRateStage(PipelineStage):
    name = "simulate_short_rate"

    def __init__(self, initial_rate, steps, dt, *, paths=1, seed=None, model=None):
        self.initial_rate, self.steps, self.dt = initial_rate, steps, dt
        self.paths, self.seed, self.model = paths, seed, model

    def run(self, data, context: PipelineContext):
        model = self.model if self.model is not None else context.get("short_rate_model")
        if not isinstance(model, (VasicekModel, CIRModel)):
            raise ValueError("a VasicekModel or CIRModel is required")
        paths = model.simulate(self.initial_rate, self.steps, self.dt, paths=self.paths, seed=self.seed)
        context.set("short_rate_paths", paths)
        context.set("short_rate_simulation", {"steps": self.steps, "dt": self.dt, "paths": self.paths, "seed": self.seed})
        return data
