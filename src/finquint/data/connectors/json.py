from pathlib import Path

import pandas as pd

from ..dataset import QuantDataset
from ..interfaces import DataProvider


class JSONProvider(DataProvider):
    def __init__(self, path: str | Path, **read_options):
        self.path = Path(path)
        self.read_options = read_options

        # Prevent pandas from downcasting JSON values such as 100.0 to 100.
        self.read_options.setdefault("dtype", False)

    def load(self) -> QuantDataset:
        data = pd.read_json(self.path, **self.read_options)

        return QuantDataset(
            data,
            {
                "source": "json",
                "path": str(self.path),
            },
        )

