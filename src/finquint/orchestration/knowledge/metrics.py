"""MetricStore: where a {{metric:ARTIFACT_ID:FIELD}} placeholder gets its value.

Read-only views over stores that already exist, so no value is copied:

    run:RUN_ID/TASK_ID           a succeeded task's output in an autonomous run (RunStore)
    experiment:EXPERIMENT_ID     an ExperimentRecord in ResearchStore (metrics, parameters, ...)
    evidence:EVIDENCE_ID@VER     an evidence item's claims (verified or authoritative tiers only)
    derived:NAME                 a value computed by a fixed deterministic method (DerivedMetrics);
                                 the value is under both "value" and NAME

A derived number (a difference, a ratio) is never computed inside a placeholder. It is computed
once by DerivedMetrics.derive() from other artifacts, stored with its inputs and method, and
then referenced like any other artifact.
"""
import json
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from ..verify.numbers import resolve


@runtime_checkable
class MetricStore(Protocol):
    def get(self, artifact_id: str):
        """The artifact as plain JSON (dict), or None."""


def _plain(value):
    return json.loads(json.dumps(value))


def _number(text):
    """Evidence claims are stored as text; numeric ones come back as numbers."""
    try:
        return int(text) if text.lstrip("-").isdigit() else float(text)
    except ValueError:
        return text


class DictMetricStore:
    """Fixed values, for tests and eval tasks."""

    def __init__(self, artifacts=None):
        self._artifacts = {k: _plain(v) for k, v in dict(artifacts or {}).items()}

    def get(self, artifact_id):
        value = self._artifacts.get(artifact_id)
        return None if value is None else _plain(value)


class RunOutputMetrics:
    """run:RUN_ID/TASK_ID -> the task's output, only if the task succeeded and the run completed."""

    def __init__(self, run_store):
        self.runs = run_store

    def get(self, artifact_id):
        if not artifact_id.startswith("run:") or artifact_id.count("/") != 1:
            return None
        run_id, task_id = artifact_id[4:].split("/")
        try:
            state = self.runs.load(run_id)
        except KeyError:
            return None
        if state.get("status") != "completed":
            return None
        task = state["tasks"].get(task_id)
        if not task or task["status"] != "succeeded":
            return None
        return _plain(task["output"])


class ExperimentMetrics:
    """experiment:ID -> the ExperimentRecord, only if its status is "verified"."""

    def __init__(self, research_store):
        self.db = research_store.connection

    def get(self, artifact_id):
        if not artifact_id.startswith("experiment:"):
            return None
        row = self.db.execute("SELECT payload FROM experiments WHERE experiment_id = ?",
                              (artifact_id[len("experiment:"):],)).fetchone()
        record = json.loads(row[0]) if row else None
        return record if record and record.get("status") == "verified" else None


class EvidenceMetrics:
    """evidence:ID@VERSION -> the item's claims. Provisional and working items are refused."""

    def __init__(self, evidence_store):
        self.store = evidence_store

    def get(self, artifact_id):
        if not artifact_id.startswith("evidence:") or "@" not in artifact_id:
            return None
        evidence_id, version = artifact_id[len("evidence:"):].rsplit("@", 1)
        item = self.store.get(evidence_id, version)
        current = self.store.current(evidence_id)
        if item is None or current is None or current.ref != item.ref:     # superseded or demoted
            return None
        if item.tier not in ("authoritative", "verified") or item.deprecated:
            return None
        return {key: _number(value) for key, value in item.claims.items()}


DERIVE_METHODS = {
    "difference": lambda a, b: a - b,
    "ratio": lambda a, b: a / b,
    "percent_change": lambda a, b: (a - b) / b,
    "sum": lambda a, b: a + b,
}
RESERVED_NAMES = ("value", "method", "inputs")


class DerivedMetrics:
    """derived:NAME -> {"value": ..., "method": ..., "inputs": [...]}.

    derive() is the only way to put a derived number in a report: it reads both inputs through
    the source store, applies a fixed method, and records which artifacts it came from.
    Derived values live in memory for one process; derive them again in a new process.
    """

    def __init__(self, source):
        self.source = source
        self._values = {}

    def derive(self, name, method, a, b):
        """a, b: (artifact_id, field). Returns the new artifact ID."""
        if method not in DERIVE_METHODS:
            raise ValueError(f"method must be one of {sorted(DERIVE_METHODS)}")
        if name in RESERVED_NAMES:
            raise ValueError(f"{name!r} is reserved; choose another name")
        artifact_id = f"derived:{name}"
        if artifact_id in self._values:
            raise ValueError(f"{artifact_id} already exists; derived values are immutable")
        left, right = resolve(self.source, *a), resolve(self.source, *b)
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (left, right)):
            raise ValueError("derive needs two numbers")
        try:
            value = DERIVE_METHODS[method](left, right)
        except ZeroDivisionError:
            raise ValueError(f"{method} of {a} and {b} divides by zero") from None
        self._values[artifact_id] = MappingProxyType({
            "value": value, name: value, "method": method, "inputs": [f"{a[0]}:{a[1]}", f"{b[0]}:{b[1]}"]})
        return artifact_id

    def get(self, artifact_id):
        value = self._values.get(artifact_id)
        return None if value is None else _plain(dict(value))


class CompositeMetricStore:
    """Asks each store in order; the first that knows the artifact answers."""

    def __init__(self, *stores):
        self.stores = stores

    def get(self, artifact_id):
        for store in self.stores:
            value = store.get(artifact_id)
            if value is not None:
                return value
        return None
