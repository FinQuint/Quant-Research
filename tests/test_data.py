import pandas as pd
import pytest

from finquint.data import QuantDataset
from finquint.data.connectors import CSVProvider, JSONProvider, ParquetProvider
from finquint.data.normalization import normalize_dataset
from finquint.data.validation import Schema


def test_csv_json_and_parquet_providers(tmp_path):
    frame = pd.DataFrame({"Bond ID": ["A"], "Price": [100.0]})
    csv_path, json_path, parquet_path = tmp_path / "a.csv", tmp_path / "a.json", tmp_path / "a.parquet"
    frame.to_csv(csv_path, index=False)
    frame.to_json(json_path, orient="records")
    frame.to_parquet(parquet_path, index=False)
    assert CSVProvider(csv_path).load().data.equals(frame)
    assert JSONProvider(json_path).load().data.equals(frame)
    assert ParquetProvider(parquet_path).load().data.equals(frame)


def test_normalization_and_schema_validation():
    dataset = normalize_dataset(QuantDataset(pd.DataFrame({"Bond ID": ["A"], "Market Price": [100]})))
    assert list(dataset.data.columns) == ["bond_id", "market_price"]
    Schema(("bond_id", "market_price"), ("market_price",)).validate(dataset)


def test_schema_reports_missing_columns():
    with pytest.raises(ValueError, match="missing required columns: ytm"):
        Schema(("ytm",)).validate(QuantDataset(pd.DataFrame({"price": [100]})))

