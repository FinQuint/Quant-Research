"""Tracing: one span per orchestration node and tool call (spec: "Evals and observability").

Every span carries the tags run_id, task_id, code_sha and model. A child span inherits its
parent's tags unless it sets its own; a span with no parent inherits the tracer's defaults.
Parents are tracked per thread, so spans from worker threads start at the tracer defaults.

There is no OpenTelemetry dependency. Spans are kept in memory, can be saved to SQLite with
SQLiteSpanStore, and to_otlp_json() renders them in the OTLP/JSON shape an OpenTelemetry
collector accepts.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import itertools
import json
import secrets
import threading
import time
from types import MappingProxyType
from typing import Mapping

REQUIRED_TAGS = ("run_id", "task_id", "code_sha", "model")


def _attribute(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


@dataclass(frozen=True)
class Span:
    trace_id: str
    span_id: str
    parent_id: str | None
    name: str
    start: float
    end: float
    status: str               # "ok" or "error"
    attributes: Mapping
    error: str | None = None

    def missing_tags(self):
        return tuple(tag for tag in REQUIRED_TAGS
                     if self.attributes.get(tag) is None or not str(self.attributes[tag]).strip())

    def to_dict(self):
        return {"trace_id": self.trace_id, "span_id": self.span_id, "parent_id": self.parent_id,
                "name": self.name, "start": self.start, "end": self.end, "status": self.status,
                "attributes": dict(self.attributes), "error": self.error}


class _OpenSpan:
    """A span being recorded. set() adds or replaces attributes."""

    def __init__(self, span_id, parent_id, attributes, start):
        self.span_id, self.parent_id, self.start = span_id, parent_id, start
        self.attributes = attributes

    def set(self, **attributes):
        self.attributes.update({k: _attribute(v) for k, v in attributes.items()})


class Tracer:
    """tags: default values for the required tags (and any others) on root spans.
    clock: seconds since the epoch; inject a fixed clock for deterministic traces."""

    def __init__(self, *, trace_id=None, clock=time.time, **tags):
        self.trace_id = trace_id or secrets.token_hex(16)
        self.tags = {k: _attribute(v) for k, v in tags.items() if v is not None}
        self._clock = clock
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._local = threading.local()
        self._spans = []

    def _stack(self):
        if not hasattr(self._local, "stack"):
            self._local.stack = []
        return self._local.stack

    @contextmanager
    def span(self, name, **attributes):
        """Attributes set to None are left to inheritance."""
        stack = self._stack()
        parent = stack[-1] if stack else None
        inherited = dict(parent.attributes) if parent else dict(self.tags)
        inherited.update({k: _attribute(v) for k, v in attributes.items() if v is not None})
        with self._lock:
            span_id = f"{next(self._ids):016x}"
        current = _OpenSpan(span_id, parent.span_id if parent else None, inherited, float(self._clock()))
        stack.append(current)
        status, error = "ok", None
        try:
            yield current
        except BaseException as exc:
            status, error = "error", f"{type(exc).__name__}: {exc}"
            raise
        finally:
            stack.pop()
            span = Span(self.trace_id, span_id, current.parent_id, name, current.start, float(self._clock()),
                        status, MappingProxyType(dict(current.attributes)), error)
            with self._lock:
                self._spans.append(span)

    @property
    def spans(self):
        with self._lock:
            return tuple(self._spans)

    def untagged(self):
        """Spans missing any required tag."""
        return tuple(span for span in self.spans if span.missing_tags())


class _NullSpan:
    def set(self, **attributes):
        pass


class NullTracer:
    """Records nothing; the default when no tracer is configured."""

    spans = ()

    @contextmanager
    def span(self, name, **attributes):
        yield _NullSpan()

    def untagged(self):
        return ()


NULL_TRACER = NullTracer()


class SQLiteSpanStore:
    """Persists spans to a trace_spans table. Call save() from the thread that owns the
    connection (SQLite connections are single-threaded by default)."""

    def __init__(self, connection):
        self.db = connection
        self.db.execute("""
        CREATE TABLE IF NOT EXISTS trace_spans (
            trace_id TEXT NOT NULL, span_id TEXT NOT NULL, parent_id TEXT, name TEXT NOT NULL,
            start REAL NOT NULL, end REAL NOT NULL, status TEXT NOT NULL, attributes TEXT NOT NULL,
            error TEXT, PRIMARY KEY (trace_id, span_id))""")
        self.db.commit()

    def save(self, spans):
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO trace_spans VALUES (?,?,?,?,?,?,?,?,?)",
                                [(s.trace_id, s.span_id, s.parent_id, s.name, s.start, s.end, s.status,
                                  json.dumps(dict(s.attributes), sort_keys=True), s.error) for s in spans])

    def load(self, trace_id):
        rows = self.db.execute("SELECT trace_id,span_id,parent_id,name,start,end,status,attributes,error "
                               "FROM trace_spans WHERE trace_id=? ORDER BY rowid", (trace_id,)).fetchall()
        return tuple(Span(*tuple(r)[:7], MappingProxyType(json.loads(r[7])), r[8]) for r in rows)


def _otlp_value(value):
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": "" if value is None else str(value)}


def to_otlp_json(spans, *, service_name="finquint-orchestration"):
    """Spans in the OTLP/JSON trace format (status code 1 = OK, 2 = ERROR)."""
    def nanos(seconds):
        return str(int(round(seconds * 1e9)))

    rendered = []
    for span in spans:
        item = {"traceId": span.trace_id, "spanId": span.span_id, "name": span.name, "kind": 1,
                "startTimeUnixNano": nanos(span.start), "endTimeUnixNano": nanos(span.end),
                "attributes": [{"key": k, "value": _otlp_value(v)} for k, v in sorted(span.attributes.items())],
                "status": {"code": 2, "message": span.error or ""} if span.status == "error" else {"code": 1}}
        if span.parent_id:
            item["parentSpanId"] = span.parent_id
        rendered.append(item)
    return {"resourceSpans": [{
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service_name}}]},
        "scopeSpans": [{"scope": {"name": "finquint.orchestration"}, "spans": rendered}]}]}
