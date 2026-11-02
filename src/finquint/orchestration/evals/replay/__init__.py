"""Replay harness (spec: "re-run recorded traces with recorded tool results").

A Recorder wraps every tool handler, reconcile check and method execution in a golden run.

- record: call through, and keep each call's input digest and its output or error.
- replay: never call through. Serve the recorded result for the same input, in recorded
  order. A call with no recorded match is a ReplayMiss: the orchestration asked for
  something the recording never did.
- live: call through and only count calls.

What is replayed is the outside world (tools, data, agents). Gates, invariants, the ledger,
approvals and the scheduler always run for real, so a replay tests the orchestration code
as it is now against the world as it was then.

A trace file holds the calls plus the run's outcome (facts and audit event types). Replay
compares the new outcome with the recorded one and lists every divergence.
"""
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
import functools
import json
from pathlib import Path
import threading

from finquint.autonomous.models import NeedsRevision, RetryableError, canonical, digest

from ...gate.decide import ProposedAction

TRACE_FORMAT = 1
TRACES_DIR = Path(__file__).resolve().parent / "traces"

# Errors a recorded call may re-raise, by class name. Anything else replays as RuntimeError.
REPLAYABLE_ERRORS = {cls.__name__: cls for cls in (RetryableError, NeedsRevision, ValueError, TypeError,
                                                   RuntimeError, ConnectionError, TimeoutError, PermissionError)}


class ReplayMiss(LookupError):
    """Replay asked for a call the recording does not contain."""


def _jsonable(value):
    if isinstance(value, ProposedAction):
        return {"proposed_action": value.to_dict()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return value


def call_key(kind, name, args):
    return digest([kind, name, _jsonable(list(args))])


def _copy(value):
    return None if value is None else json.loads(canonical(value))


class Recorder:
    MODES = ("live", "record", "replay")

    def __init__(self, mode="live", calls=()):
        if mode not in self.MODES:
            raise ValueError(f"mode must be one of {self.MODES}")
        self.mode = mode
        self.calls = [] if mode != "replay" else [dict(c) for c in calls]
        self._queues = defaultdict(deque)
        for call in self.calls:
            self._queues[call["key"]].append(call)
        self._lock = threading.Lock()
        self.counts = Counter()        # (kind, name) -> calls made
        self.keys = defaultdict(list)  # (kind, name) -> input keys, in call order
        self.misses = []

    def wrap(self, kind, name, fn):
        """fn's inputs and output must be JSON (ProposedAction is converted). functools.wraps
        keeps fn's name and source, so a wrapped Method keeps its signature."""
        @functools.wraps(fn)
        def wrapper(*args):
            key = call_key(kind, name, args)
            with self._lock:
                self.counts[(kind, name)] += 1
                self.keys[(kind, name)].append(key)
            if self.mode == "replay":
                return self._serve(kind, name, key)
            try:
                output = fn(*args)
            except Exception as exc:
                if self.mode == "record":
                    self._keep(kind, name, key, error={"type": type(exc).__name__, "message": str(exc)})
                raise
            if self.mode == "record":
                self._keep(kind, name, key, output=_copy(output))
            return output
        return wrapper

    def _keep(self, kind, name, key, *, output=None, error=None):
        with self._lock:
            self.calls.append({"kind": kind, "name": name, "key": key, "output": output, "error": error})

    def _serve(self, kind, name, key):
        with self._lock:
            queue = self._queues.get(key)
            if not queue:
                miss = f"no recorded {kind} call {name} for this input ({key[:12]})"
                self.misses.append(miss)
                raise ReplayMiss(miss)
            call = queue.popleft()
        if call["error"] is not None:
            raise REPLAYABLE_ERRORS.get(call["error"]["type"], RuntimeError)(call["error"]["message"])
        return _copy(call["output"])

    def unused(self):
        """Recorded calls replay never asked for."""
        with self._lock:
            return [c for queue in self._queues.values() for c in queue]

    def invocations(self, kind, name):
        return self.counts[(kind, name)]

    def duplicates(self, kind, name):
        """Calls repeated with the same input (for a side effect: the same action twice)."""
        return sum(n - 1 for n in Counter(self.keys[(kind, name)]).values())


@dataclass(frozen=True)
class Trace:
    task: str
    calls: tuple
    outcome: dict

    def to_dict(self):
        return {"format": TRACE_FORMAT, "task": self.task, "calls": list(self.calls), "outcome": self.outcome}

    @classmethod
    def from_dict(cls, data):
        if data.get("format") != TRACE_FORMAT:
            raise ValueError(f"unsupported trace format: {data.get('format')}")
        return cls(data["task"], tuple(data["calls"]), data["outcome"])

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=1, sort_keys=True) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path):
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def trace_path(task_name, directory=TRACES_DIR):
    return Path(directory) / f"{task_name}.json"


def diff_outcomes(expected, actual):
    """Human-readable differences between two outcomes ({"facts": {...}, "events": [...]})."""
    found = []
    old_facts, new_facts = expected.get("facts", {}), actual.get("facts", {})
    for key in sorted(set(old_facts) | set(new_facts)):
        if key not in new_facts:
            found.append(f"fact {key}: missing (was {old_facts[key]!r})")
        elif key not in old_facts:
            found.append(f"fact {key}: new ({new_facts[key]!r})")
        elif canonical(old_facts[key]) != canonical(new_facts[key]):
            found.append(f"fact {key}: {old_facts[key]!r} -> {new_facts[key]!r}")
    old_events, new_events = expected.get("events", []), actual.get("events", [])
    if old_events != new_events:
        index = next((i for i, (a, b) in enumerate(zip(old_events, new_events)) if a != b),
                     min(len(old_events), len(new_events)))
        was = old_events[index] if index < len(old_events) else "(end)"
        now = new_events[index] if index < len(new_events) else "(end)"
        found.append(f"events differ at #{index}: {was} -> {now} "
                     f"({len(old_events)} recorded, {len(new_events)} now)")
    return found
