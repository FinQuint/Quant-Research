"""Template types: a strategy template is named strategy code with typed, bounded parameters.

Kept apart from the package's __init__ so an agent-written template module can import these
without importing the registry it is about to be added to (Phase 10).
"""
from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType

PARAM_KINDS = ("integer", "number", "boolean")


@dataclass(frozen=True)
class Param:
    kind: str                  # "integer", "number" or "boolean"
    minimum: float = None
    maximum: float = None
    default: object = None

    def check(self, name, value):
        if self.kind == "boolean":
            if type(value) is not bool:
                raise ValueError(f"{name} must be true or false")
            return
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
            raise ValueError(f"{name} must be a finite number")
        if self.kind == "integer" and not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
        if not self.minimum <= value <= self.maximum:
            raise ValueError(f"{name}={value} is outside [{self.minimum}, {self.maximum}]")

    def to_dict(self):
        out = {"type": self.kind, "default": self.default}
        if self.kind != "boolean":
            out.update(min=self.minimum, max=self.maximum)
        return out


@dataclass(frozen=True)
class StrategyTemplate:
    name: str
    description: str
    params: dict
    factory: object            # factory(symbol, **params) -> Strategy

    def __post_init__(self):
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))

    def resolve(self, values):
        """Defaults filled in; unknown or out-of-bounds parameters raise ValueError."""
        if not isinstance(values, dict):
            raise ValueError("strategy parameters must be an object")
        unknown = set(values) - set(self.params)
        if unknown:
            raise ValueError(f"unknown parameters for {self.name}: {sorted(unknown)}")
        out = {}
        for name, spec in self.params.items():
            value = values.get(name, spec.default)
            spec.check(name, value)
            out[name] = value
        return out

    def build(self, symbol, values):
        return self.factory(symbol, **self.resolve(values))

    def describe(self):
        return {"name": self.name, "description": self.description,
                "params": {k: v.to_dict() for k, v in sorted(self.params.items())}}


__all__ = ["PARAM_KINDS", "Param", "StrategyTemplate"]
