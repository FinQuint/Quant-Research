"""Host-owned allowlists for analytics, research and supervisor gates."""
from dataclasses import dataclass
from hashlib import sha256
import inspect
from typing import Callable

from finquint.agents import ValidationResult
from .models import canonical


@dataclass(frozen=True)
class Method:
    name: str
    version: str
    role: str
    execute: Callable
    validate_input: Callable
    gates: tuple[Callable, ...]
    requires_approval: bool = False

    def __post_init__(self):
        if not self.name.strip() or not self.version.strip() or self.role not in {"research", "execution", "proposal"}:
            raise ValueError("method requires name, version and an allowed role")
        if not callable(self.execute) or not callable(self.validate_input) or not self.gates or not all(callable(g) for g in self.gates):
            raise ValueError("method requires execution, input validator and mandatory gates")
        if type(self.requires_approval) is not bool:
            raise ValueError("requires_approval must be boolean")

    def signature(self):
        def identity(function):
            name = f"{getattr(function, '__module__', '')}.{getattr(function, '__qualname__', type(function).__qualname__)}"
            try:
                source = inspect.getsource(function)
            except (OSError, TypeError):
                source = name
            return {"name": name, "source_sha256": sha256(source.encode()).hexdigest()}
        return {"version": self.version, "role": self.role,
                "requires_approval": self.requires_approval or self.role == "proposal",
                "execute": identity(self.execute), "input_validator": identity(self.validate_input),
                "gates": [identity(gate) for gate in self.gates]}


class MethodRegistry:
    def __init__(self):
        self._methods = {}

    def register(self, method):
        if not isinstance(method, Method):
            raise TypeError("expected Method")
        if method.name in self._methods:
            raise ValueError("method already registered")
        self._methods[method.name] = method
        return self

    def get(self, name):
        if name not in self._methods:
            raise ValueError(f"method is not approved: {name}")
        return self._methods[name]


class Supervisor:
    """Mandatory structural/provenance checks plus independent numerical gates."""

    def validate(self, method, parameters, context, output):
        canonical(output)  # rejects NaN/Infinity and non-JSON values, at any depth
        if not isinstance(output, dict) or not output:
            raise ValueError("method returned an empty or invalid output")
        if method.role == "research":
            findings = output.get("findings")
            if not isinstance(findings, list) or not findings:
                raise ValueError("research requires source-backed findings")
            sources = {s["source_id"]: s for s in context["sources"]}
            for finding in findings:
                source = sources.get(finding.get("source_id"))
                quote = finding.get("quote")
                if source is None or not isinstance(quote, str) or not quote.strip() or quote not in source["content"]:
                    raise ValueError("research finding lacks a verifiable source excerpt")
                if set(finding) != {"source_id", "quote"}:
                    raise ValueError("free-form interpretations require separate human review")
            if set(output) != {"findings"}:
                raise ValueError("research output must contain only cited findings")
        if method.role == "proposal":
            if set(output) != {"patch", "rationale"} or not all(isinstance(v, str) and v.strip() for v in output.values()):
                raise ValueError("proposal must contain inert patch text and rationale")
        results = []
        for gate in method.gates:
            # Gates receive independent copies; no gate can rewrite another's evidence.
            import json
            try:
                result = gate(*json.loads(canonical([parameters, context, output])))
            except Exception as exc:
                # A validation error is never an execution retry request.
                raise ValueError(f"hard validation gate raised {type(exc).__name__}: {exc}") from exc
            if not isinstance(result, ValidationResult) or type(result.passed) is not bool:
                raise TypeError("gate must return a boolean ValidationResult")
            results.append({"name": result.name, "passed": result.passed,
                            "details": result.details, "metrics": dict(result.metrics)})
        if any(not r["passed"] for r in results):
            raise ValueError(f"hard validation gate failed: {results}")
        return results
