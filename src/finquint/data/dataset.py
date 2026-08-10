from dataclasses import dataclass, field
from typing import Any
import pandas as pd

@dataclass
class QuantDataset:
    data: pd.DataFrame
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self):
        if not isinstance(self.data, pd.DataFrame):
            raise TypeError("data must be a pandas DataFrame")
        if self.data.columns.duplicated().any():
            raise ValueError("duplicate column names")
