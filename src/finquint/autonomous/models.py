"""JSON-only contracts: model-generated requests never become executable code."""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode()).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not value or not all(c.isalnum() or c in "_-" for c in value):
        raise ValueError("identifiers may contain only letters, numbers, underscores and hyphens")
    return value


class RetryableError(Exception):
    """Explicit transient failure; only trusted adapters may request a retry."""


class NeedsRevision(Exception):
    """Prompt, method or assumptions cannot be adopted; requires a revised plan."""


@dataclass(frozen=True)
class Source:
    source_id: str
    uri: str
    version: str
    content: str

    def __post_init__(self):
        identifier(self.source_id)
        if not all(isinstance(v, str) and v.strip() for v in (self.uri, self.version, self.content)):
            raise ValueError("source URI, immutable version and content are required")

    def snapshot(self):
        return dict(asdict(self), sha256=sha256(self.content.encode()).hexdigest())


@dataclass(frozen=True)
class Call:
    task_id: str
    method: str
    parameters: dict
    dependencies: tuple[str, ...] = ()

    def __post_init__(self):
        identifier(self.task_id)
        if not self.method.strip() or not isinstance(self.parameters, dict):
            raise ValueError("method and parameter object required")
        canonical(self.parameters)


@dataclass(frozen=True)
class Request:
    run_id: str
    objective: str
    code_version: str
    calls: tuple[Call, ...]
    sources: tuple[Source, ...]
    max_attempts: int = 2

    def __post_init__(self):
        identifier(self.run_id)
        if not self.objective.strip() or not self.code_version.strip() or not self.calls or not self.sources:
            raise ValueError("objective, code version, calls and source snapshots are required")
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        ids = {c.task_id for c in self.calls}
        if len(ids) != len(self.calls) or len({s.source_id for s in self.sources}) != len(self.sources):
            raise ValueError("duplicate task or source identifiers")
        visited, visiting = set(), set()
        graph = {c.task_id: c.dependencies for c in self.calls}

        def visit(key):
            if key not in graph or key in visiting:
                raise ValueError("unknown dependency or cyclic plan")
            if key in visited:
                return
            visiting.add(key)
            for dep in graph[key]:
                visit(dep)
            visiting.remove(key)
            visited.add(key)
        for key in graph:
            visit(key)


class Planner:
    """A deterministic planner; a host may translate LLM output to Request first."""

    def build(self, request, registry):
        # Snapshot mutable caller inputs before validation and persistence.
        plan = json.loads(canonical(asdict(request)))
        plan["sources"] = [s.snapshot() for s in request.sources]
        plan["methods"] = {}
        for call in plan["calls"]:
            method = registry.get(call["method"])
            method.validate_input(call["parameters"])
            plan["methods"][method.name] = method.signature()
        if not any(registry.get(c["method"]).role == "execution" for c in plan["calls"]):
            raise ValueError("plan must contain deterministic execution")
        if not any(registry.get(c["method"]).role == "research" for c in plan["calls"]):
            raise ValueError("plan must contain read-only research")
        return plan
