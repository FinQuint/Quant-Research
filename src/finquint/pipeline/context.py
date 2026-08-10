from dataclasses import dataclass, field
from typing import Any

@dataclass
class PipelineContext:
    metadata: dict[str, Any] = field(default_factory=dict)
    results: dict[str, Any] = field(default_factory=dict)

    def set(self, key, value): self.results[key] = value
    def get(self, key, default=None): return self.results.get(key, default)
