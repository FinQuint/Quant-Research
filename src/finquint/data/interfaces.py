from abc import ABC, abstractmethod

from .dataset import QuantDataset


class DataProvider(ABC):
    @abstractmethod
    def load(self) -> QuantDataset:
        """Load source data into the standard dataset contract."""

