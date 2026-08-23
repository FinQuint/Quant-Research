from dataclasses import dataclass, field
from typing import Any

import pandas as pd


@dataclass
class QuantDataset:
    data: pd.DataFrame
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.data, pd.DataFrame):
            raise TypeError("data must be a pandas DataFrame")
        self.metadata.setdefault("rows", len(self.data))
        self.metadata.setdefault("columns", list(self.data.columns))

