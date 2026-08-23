import re

from .dataset import QuantDataset


def normalize_dataset(dataset: QuantDataset, dtypes: dict[str, str] | None = None) -> QuantDataset:
    frame = dataset.data.copy()
    frame.columns = [re.sub(r"[^a-z0-9]+", "_", str(c).strip().lower()).strip("_") for c in frame.columns]
    if dtypes:
        frame = frame.astype(dtypes)
    metadata = {**dataset.metadata, "normalized": True}
    return QuantDataset(frame, metadata)

