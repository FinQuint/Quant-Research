from pathlib import Path

import pandas as pd

from ..dataset import QuantDataset
from ..interfaces import DataProvider


class CSVProvider(DataProvider):
    def __init__(self, path: str | Path, **read_options):
        self.path = Path(path)
        self.read_options = read_options

    def load(self) -> QuantDataset:
        data = pd.read_csv(self.path, **self.read_options)
        return QuantDataset(data, {"source": "csv", "path": str(self.path)})

