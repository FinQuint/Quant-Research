"""Recovery policy (configs/recovery_policy.json): what the ladder may do, as reviewed data (ADR-020).

All numbers here are (tune) values. A method with no rule is never repaired. Known fixes are
human-approved deterministic rules; the ladder applies them without a model call.
"""
from dataclasses import dataclass, field
import json
from math import isfinite
from pathlib import Path
from types import MappingProxyType

from finquint.autonomous.models import canonical

CONFIG_PATH = Path(__file__).resolve().parents[4] / "configs" / "recovery_policy.json"


def _int(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    return value


@dataclass(frozen=True)
class Bound:
    """A tunable parameter's allowed values: a numeric range (optionally integer-only) or a list."""
    minimum: float = None
    maximum: float = None
    integer: bool = False
    values: tuple = None

    def __post_init__(self):
        if self.values is not None:
            if self.minimum is not None or self.maximum is not None:
                raise ValueError("a bound is either a range or a list of values")
            object.__setattr__(self, "values", tuple(self.values))
            if not self.values:
                raise ValueError("a value list must not be empty")
            canonical(list(self.values))
            return
        for name in ("minimum", "maximum"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
                raise ValueError(f"bound {name} must be a finite number")
        if self.minimum > self.maximum:
            raise ValueError("bound minimum exceeds maximum")
        if type(self.integer) is not bool:
            raise ValueError("integer must be boolean")

    def allows(self, value):
        if self.values is not None:
            return any(canonical(value) == canonical(v) for v in self.values)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        if self.integer and not isinstance(value, int):
            return False
        return isfinite(value) and self.minimum <= value <= self.maximum

    def to_dict(self):
        if self.values is not None:
            return {"values": list(self.values)}
        return {"min": self.minimum, "max": self.maximum, **({"type": "integer"} if self.integer else {})}

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError("a bound must be an object")
        if "values" in data:
            if set(data) != {"values"}:
                raise ValueError("a value-list bound takes only values")
            return cls(values=tuple(data["values"]))
        if not set(data) <= {"min", "max", "type"} or data.get("type", "integer") != "integer":
            raise ValueError("a range bound takes min, max and optionally type: integer")
        return cls(data.get("min"), data.get("max"), integer="type" in data)


@dataclass(frozen=True)
class RecoveryRule:
    """tunable: dotted parameter path -> Bound. frozen: paths that must never change (documented and
    checked; any other non-tunable path is also held fixed). alternatives: registered methods with
    the same role and parameter contract."""
    tunable: dict = field(default_factory=dict)
    frozen: tuple = ()
    alternatives: tuple = ()

    def __post_init__(self):
        tunable = dict(self.tunable)
        if not all(isinstance(k, str) and k.strip() and isinstance(v, Bound) for k, v in tunable.items()):
            raise ValueError("tunable maps parameter paths to Bounds")
        frozen, alternatives = tuple(self.frozen), tuple(self.alternatives)
        if not all(isinstance(p, str) and p.strip() for p in frozen + alternatives):
            raise ValueError("frozen paths and alternatives must be non-empty strings")
        if set(tunable) & set(frozen):
            raise ValueError("a parameter cannot be both tunable and frozen")
        if not tunable and not alternatives:
            raise ValueError("a recovery rule needs a tunable parameter or an alternative method")
        object.__setattr__(self, "tunable", MappingProxyType(tunable))
        object.__setattr__(self, "frozen", frozen)
        object.__setattr__(self, "alternatives", alternatives)


@dataclass(frozen=True)
class KnownFix:
    """A human-approved fix: when `method` fails with `signature`, apply `change` with no model call.
    change: {"method": name} and/or {"parameters": {path: value}}."""
    method: str
    signature: str
    change: dict

    def __post_init__(self):
        if not all(isinstance(v, str) and v.strip() for v in (self.method, self.signature)):
            raise ValueError("a known fix needs a method and a failure signature")
        change = self.change
        if not isinstance(change, dict) or not change or not set(change) <= {"method", "parameters"}:
            raise ValueError("a known fix change takes method and/or parameters")
        if "parameters" in change and (not isinstance(change["parameters"], dict) or not change["parameters"]):
            raise ValueError("known fix parameters must be a nonempty object")
        canonical(change)

    def to_dict(self):
        return {"method": self.method, "signature": self.signature, "change": self.change}


@dataclass(frozen=True)
class RecoveryPolicy:
    methods: dict = field(default_factory=dict)
    known_fixes: tuple = ()
    max_repair_attempts: int = 2
    validator_sample_rate: float = 0.0
    validator_model: str = None
    lesson_threshold: int = 3
    breaker_trips: int = 3
    breaker_window: int = 10
    cost_cap_usd: float = 0.5
    version: str = "1"

    def __post_init__(self):
        methods = dict(self.methods)
        if not all(isinstance(k, str) and isinstance(v, RecoveryRule) for k, v in methods.items()):
            raise ValueError("methods maps method names to RecoveryRules")
        fixes = tuple(self.known_fixes)
        if not all(isinstance(f, KnownFix) for f in fixes):
            raise ValueError("known_fixes must be KnownFix items")
        _int(self.max_repair_attempts, "max_repair_attempts", 1)
        _int(self.lesson_threshold, "lesson_threshold", 1)
        _int(self.breaker_window, "breaker_window", 1)
        _int(self.breaker_trips, "breaker_trips", 1)
        if self.breaker_trips > self.breaker_window:
            raise ValueError("breaker_trips cannot exceed breaker_window")
        rate = self.validator_sample_rate
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0 <= rate <= 1:
            raise ValueError("validator_sample_rate must lie in [0, 1]")
        cap = self.cost_cap_usd
        if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not isfinite(cap) or cap < 0:
            raise ValueError("cost_cap_usd must be a non-negative number")
        if self.validator_model is not None and (not isinstance(self.validator_model, str)
                                                 or not self.validator_model.strip()):
            raise ValueError("validator_model must be a name or null")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("policy version is required")
        object.__setattr__(self, "methods", MappingProxyType(methods))
        object.__setattr__(self, "known_fixes", fixes)

    def rule(self, method):
        return self.methods.get(method)

    def known_fix(self, method, signature):
        return next((f for f in self.known_fixes if f.method == method and f.signature == signature), None)

    @classmethod
    def from_dict(cls, data):
        methods = {name: RecoveryRule(tunable={p: Bound.from_dict(b) for p, b in rule.get("tunable", {}).items()},
                                      frozen=tuple(rule.get("frozen", ())),
                                      alternatives=tuple(rule.get("alternatives", ())))
                   for name, rule in data.get("methods", {}).items()}
        breaker = data.get("circuit_breaker", {})
        return cls(methods=methods,
                   known_fixes=tuple(KnownFix(f["method"], f["signature"], f["change"])
                                     for f in data.get("known_fixes", ())),
                   max_repair_attempts=data.get("max_repair_attempts", 2),
                   validator_sample_rate=data.get("validator_sample_rate", 0.0),
                   validator_model=data.get("validator_model"),
                   lesson_threshold=data.get("lesson_threshold", 3),
                   breaker_trips=breaker.get("trips", 3), breaker_window=breaker.get("window", 10),
                   cost_cap_usd=data.get("cost_cap_usd", 0.5), version=str(data.get("version", "1")))

    @classmethod
    def from_file(cls, path=CONFIG_PATH):
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
