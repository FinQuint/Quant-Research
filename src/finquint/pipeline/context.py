from dataclasses import dataclass, field
from typing import Any


@dataclass
class PipelineContext:
    metadata: dict[str, Any] = field(default_factory=dict)
    results: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, str]] = field(default_factory=list)

    def set(self, key: str, value: Any) -> None:
        self.results[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.results.get(key, default)

