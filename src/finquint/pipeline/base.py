from abc import ABC, abstractmethod
from typing import Any

from .context import PipelineContext


class PipelineStage(ABC):
    """A composable unit of work in a quantitative research pipeline."""

    name = "unnamed_stage"

    @abstractmethod
    def run(self, data: Any, context: PipelineContext) -> Any:
        """Transform data and/or add values to the shared context."""

