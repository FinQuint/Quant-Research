from abc import ABC, abstractmethod
from .dataset import QuantDataset

class DataProvider(ABC):
    @abstractmethod
    def load(self) -> QuantDataset:
        raise NotImplementedError
