from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from enum import Enum
from math import isfinite
from typing import Mapping

import pandas as pd

from finquint.data import QuantDataset


class Severity(str, Enum):
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    passed: bool
    severity: Severity
    evidence: str
    remediation: str
    metrics: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self):
        if not self.check_id.strip() or not self.evidence.strip() or not self.remediation.strip():
            raise ValueError("check id, evidence, and remediation are required")
        if any(not isfinite(float(value)) for value in self.metrics.values()):
            raise ValueError("check metrics must be finite")


@dataclass(frozen=True)
class EvaluationContext:
    frame: pd.DataFrame
    metadata: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def from_data(cls, data, metadata=None):
        if isinstance(data, QuantDataset):
            frame = data.data
            combined = dict(data.metadata)
            combined.update(metadata or {})
        elif isinstance(data, pd.DataFrame):
            frame, combined = data, dict(metadata or {})
        else:
            raise TypeError("evaluation data must be QuantDataset or DataFrame")
        return cls(frame.copy(), combined)


@dataclass(frozen=True)
class EvaluationReport:
    results: tuple[CheckResult, ...]

    @property
    def passed(self) -> bool:
        return not any(not result.passed and result.severity == Severity.ERROR for result in self.results)

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(result.check_id for result in self.results if not result.passed and result.severity == Severity.WARNING)

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(result.check_id for result in self.results if not result.passed and result.severity == Severity.ERROR)

    def to_dict(self):
        return {"passed": self.passed, "failures": self.failures, "warnings": self.warnings,
                "results": [dict(asdict(result), severity=result.severity.value) for result in self.results]}


class QuantResearchCheck(ABC):
    check_id: str

    @abstractmethod
    def evaluate(self, context: EvaluationContext) -> CheckResult:
        """Return evidence; checks must not mutate data or metadata."""


class ResearchEvaluationSuite:
    def __init__(self, checks):
        self.checks = tuple(checks)
        if not self.checks or any(not isinstance(check, QuantResearchCheck) for check in self.checks):
            raise ValueError("provide QuantResearchCheck objects")
        ids = [check.check_id for check in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("evaluation check ids must be unique")

    def evaluate(self, data, *, metadata=None) -> EvaluationReport:
        context = EvaluationContext.from_data(data, metadata)
        return EvaluationReport(tuple(check.evaluate(context) for check in self.checks))
