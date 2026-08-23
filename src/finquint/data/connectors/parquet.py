from pathlib import Path

import pandas as pd

from ..dataset import QuantDataset
from ..interfaces import DataProvider


class ParquetProvider(DataProvider):
    def __init__(self, path: str | Path, **read_options):
        self.path = Path(path)
        self.read_options = read_options

    def load(self) -> QuantDataset:
        data = pd.read_parquet(self.path, **self.read_options)
        return QuantDataset(data, {"source": "parquet", "path": str(self.path)})

