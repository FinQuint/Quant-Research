"""Coding policy (configs/coding_policy.json, protected): limits and the import allowlist (ADR-023).

Every number here is (tune). allowed_imports maps a module to "*" (any name, plain import allowed)
or to the list of names that may be imported from it (no plain import).
"""
from dataclasses import dataclass, field
import json
from pathlib import Path
from types import MappingProxyType

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG_PATH = REPO_ROOT / "configs" / "coding_policy.json"
_POSITIVE_INTS = ("max_revision_attempts", "max_module_lines", "output_cap_bytes", "lookahead_cutoffs")
_POSITIVE_NUMBERS = ("sandbox_timeout_seconds", "max_backtest_seconds")


@dataclass(frozen=True)
class CodingPolicy:
    allowed_imports: dict = field(default_factory=dict)
    max_revision_attempts: int = 2
    max_module_lines: int = 200
    sandbox_timeout_seconds: float = 60
    max_backtest_seconds: float = 20
    output_cap_bytes: int = 65536
    conformance_dataset: str = "demo_prices@1"
    lookahead_cutoffs: int = 3
    branch_prefix: str = "agent/"
    version: str = "1"

    def __post_init__(self):
        for name in _POSITIVE_INTS:
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in _POSITIVE_NUMBERS:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
                raise ValueError(f"{name} must be a positive number")
        if not isinstance(self.branch_prefix, str) or not self.branch_prefix.endswith("/") or not self.branch_prefix[:-1]:
            raise ValueError("branch_prefix must look like 'agent/'")
        imports = {}
        for module, names in dict(self.allowed_imports).items():
            if names == "*":
                imports[module] = "*"
            elif isinstance(names, (list, tuple)) and names and all(isinstance(n, str) and n for n in names):
                imports[module] = frozenset(names)
            else:
                raise ValueError(f"allowed_imports[{module!r}] must be '*' or a nonempty list of names")
        object.__setattr__(self, "allowed_imports", MappingProxyType(imports))

    @classmethod
    def from_dict(cls, data):
        known = {k: data[k] for k in ("max_revision_attempts", "max_module_lines", "sandbox_timeout_seconds",
                                      "max_backtest_seconds", "output_cap_bytes", "conformance_dataset",
                                      "lookahead_cutoffs", "branch_prefix") if k in data}
        return cls(dict(data.get("allowed_imports", {})), version=str(data.get("version", "1")), **known)

    @classmethod
    def from_file(cls, path=CONFIG_PATH):
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


__all__ = ["CONFIG_PATH", "CodingPolicy", "REPO_ROOT"]
