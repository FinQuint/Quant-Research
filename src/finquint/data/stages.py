from typing import Any

from finquint.pipeline import PipelineContext, PipelineStage

from .dataset import QuantDataset
from .interfaces import DataProvider
from .normalization import normalize_dataset
from .validation import Schema


class LoadDataStage(PipelineStage):
    name = "load_data"

    def __init__(self, provider: DataProvider):
        self.provider = provider

    def run(self, data: Any, context: PipelineContext) -> QuantDataset:
        dataset = self.provider.load()
        context.metadata["source"] = dataset.metadata
        return dataset


class NormalizeDataStage(PipelineStage):
    name = "normalize_data"

    def __init__(self, dtypes: dict[str, str] | None = None):
        self.dtypes = dtypes

    def run(self, data: QuantDataset, context: PipelineContext) -> QuantDataset:
        if not isinstance(data, QuantDataset):
            raise TypeError("NormalizeDataStage requires QuantDataset input")
        return normalize_dataset(data, self.dtypes)


class ValidateDataStage(PipelineStage):
    name = "validate_data"

    def __init__(self, schema: Schema):
        self.schema = schema

    def run(self, data: QuantDataset, context: PipelineContext) -> QuantDataset:
        if not isinstance(data, QuantDataset):
            raise TypeError("ValidateDataStage requires QuantDataset input")
        self.schema.validate(data)
        context.metadata["validated"] = True
        return data

