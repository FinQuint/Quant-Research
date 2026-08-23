from dataclasses import dataclass
from time import perf_counter
from typing import Any

from .base import PipelineStage
from .context import PipelineContext


@dataclass(frozen=True)
class PipelineResult:
    data: Any
    context: PipelineContext


class QuantPipeline:
    def __init__(self, name: str = "quant_pipeline", *, continue_on_error: bool = False):
        self.name = name
        self.continue_on_error = continue_on_error
        self.stages: list[PipelineStage] = []

    def add(self, stage: PipelineStage) -> "QuantPipeline":
        self.stages.append(stage)
        return self

    def run(self, data: Any = None, context: PipelineContext | None = None) -> PipelineResult:
        context = context or PipelineContext()
        context.metadata["pipeline_name"] = self.name
        for stage in self.stages:
            started = perf_counter()
            status = "success"
            try:
                data = stage.run(data, context)
            except Exception as exc:
                status = "failed"
                context.errors.append({"stage": stage.name, "error": str(exc)})
                if not self.continue_on_error:
                    raise
            finally:
                context.metrics[stage.name] = {
                    "duration_seconds": perf_counter() - started,
                    "status": status,
                }
        return PipelineResult(data=data, context=context)

