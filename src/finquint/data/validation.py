from dataclasses import dataclass, field

from .dataset import QuantDataset


@dataclass(frozen=True)
class Schema:
    required_columns: tuple[str, ...]
    non_null_columns: tuple[str, ...] = field(default_factory=tuple)

    def validate(self, dataset: QuantDataset) -> None:
        missing = sorted(set(self.required_columns) - set(dataset.data.columns))
        if missing:
            raise ValueError(f"missing required columns: {', '.join(missing)}")
        null_columns = [c for c in self.non_null_columns if dataset.data[c].isna().any()]
        if null_columns:
            raise ValueError(f"null values in columns: {', '.join(null_columns)}")

