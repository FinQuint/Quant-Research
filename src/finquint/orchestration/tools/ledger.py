"""Idempotency ledger and outbox (ADR-004), behind a storage interface.

SQLite is the first backend and can share the ResearchStore connection. A Postgres
backend implements the same LedgerBackend methods.

Status flow
    direct writes:  pending -> done | failed
    outbox:         queued -> dispatching -> done | failed | in_doubt
                    dispatching -> queued   (recovery proved the effect did not happen)
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json

from finquint.autonomous.models import canonical


class EffectStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    DISPATCHING = "dispatching"
    DONE = "done"
    FAILED = "failed"
    IN_DOUBT = "in_doubt"   # effect may or may not have happened; a human must resolve


TERMINAL = frozenset({EffectStatus.DONE, EffectStatus.FAILED, EffectStatus.IN_DOUBT})


def idempotency_key(run_id, action_fingerprint, attempt_scope="0"):
    """sha256 over (run_id, fingerprint, attempt_scope), encoded so fields cannot run together."""
    if not all(isinstance(v, str) and v for v in (run_id, action_fingerprint, attempt_scope)):
        raise ValueError("run_id, fingerprint and attempt_scope must be non-empty strings")
    return sha256(canonical([run_id, action_fingerprint, attempt_scope]).encode()).hexdigest()


@dataclass(frozen=True)
class LedgerEntry:
    key: str
    run_id: str
    fingerprint: str
    tool: str
    action: dict
    status: EffectStatus
    result: dict | None
    error: str | None
    attempts: int


class LedgerBackend(ABC):
    @abstractmethod
    def get(self, key): """LedgerEntry or None."""

    @abstractmethod
    def begin(self, key, *, run_id, fingerprint, tool, action):
        """Direct write: insert as pending, or re-take a stale pending row. Returns (entry, created)."""

    @abstractmethod
    def enqueue(self, key, *, run_id, fingerprint, tool, action):
        """Outbox write: ledger row + outbox row in one transaction. Returns (entry, created)."""

    @abstractmethod
    def claim_next(self):
        """Atomically move the oldest queued entry to dispatching. Returns it, or None."""

    @abstractmethod
    def requeue(self, key):
        """dispatching -> queued, only when recovery proved the effect did not happen."""

    @abstractmethod
    def finish(self, key, status, *, result=None, error=None):
        """pending|dispatching -> done|failed|in_doubt."""

    @abstractmethod
    def entries(self, *, status=None, run_id=None):
        """All entries, optionally filtered, in insertion order."""

    @abstractmethod
    def record(self, event_type, subject_id, payload):
        """Append an audit event (e.g. a refused call)."""


def _now():
    return datetime.now(timezone.utc).isoformat()


class SQLiteLedger(LedgerBackend):
    def __init__(self, connection, *, audit=None):
        """audit(event_type, subject_id, payload) runs inside the same transaction."""
        self.db, self._audit = connection, audit
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS side_effects (
            key TEXT PRIMARY KEY, run_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
            tool TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL,
            result TEXT, error TEXT, attempts INTEGER NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS outbox (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL UNIQUE REFERENCES side_effects(key),
            enqueued_at TEXT NOT NULL
        );
        """)
        self.db.commit()

    def _event(self, event_type, subject_id, payload):
        if self._audit is not None:
            self._audit(event_type, subject_id, payload)

    @staticmethod
    def _entry(row):
        if row is None:
            return None
        key, run_id, fp, tool, action, status, result, error, attempts = tuple(row)[:9]
        return LedgerEntry(key, run_id, fp, tool, json.loads(action), EffectStatus(status),
                           None if result is None else json.loads(result), error, attempts)

    def _select(self, key):
        return self._entry(self.db.execute(
            "SELECT key,run_id,fingerprint,tool,action,status,result,error,attempts "
            "FROM side_effects WHERE key=?", (key,)).fetchone())

    @staticmethod
    def _same_action(entry, fingerprint, tool):
        if entry.fingerprint != fingerprint or entry.tool != tool:
            raise ValueError("idempotency key reused for a different action")

    def get(self, key):
        return self._select(key)

    def _insert(self, key, run_id, fingerprint, tool, action, status, attempts):
        now = _now()
        self.db.execute("INSERT INTO side_effects VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (key, run_id, fingerprint, tool, canonical(action), status.value,
                         None, None, attempts, now, now))

    def _set(self, key, **fields):
        fields["updated_at"] = _now()
        assignments = ",".join(f"{name}=?" for name in fields)
        self.db.execute(f"UPDATE side_effects SET {assignments} WHERE key=?", (*fields.values(), key))

    def begin(self, key, *, run_id, fingerprint, tool, action):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            entry = self._select(key)
            if entry is None:
                self._insert(key, run_id, fingerprint, tool, action, EffectStatus.PENDING, 1)
                self._event("side_effect_started", run_id, {"key": key, "tool": tool})
                return self._select(key), True
            self._same_action(entry, fingerprint, tool)
            if entry.status is EffectStatus.PENDING:
                # Interrupted earlier; the tool class guarantees a repeat is safe.
                self._set(key, attempts=entry.attempts + 1)
                self._event("side_effect_restarted", run_id, {"key": key, "tool": tool})
                return self._select(key), False
            return entry, False

    def enqueue(self, key, *, run_id, fingerprint, tool, action):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            entry = self._select(key)
            if entry is not None:
                self._same_action(entry, fingerprint, tool)
                return entry, False
            self._insert(key, run_id, fingerprint, tool, action, EffectStatus.QUEUED, 0)
            self.db.execute("INSERT INTO outbox(key,enqueued_at) VALUES (?,?)", (key, _now()))
            self._event("side_effect_enqueued", run_id, {"key": key, "tool": tool})
            return self._select(key), True

    def claim_next(self):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT s.key FROM outbox o JOIN side_effects s ON s.key=o.key "
                "WHERE s.status=? ORDER BY o.sequence LIMIT 1", (EffectStatus.QUEUED.value,)).fetchone()
            if row is None:
                return None
            entry = self._select(row[0])
            self._set(entry.key, status=EffectStatus.DISPATCHING.value, attempts=entry.attempts + 1)
            self._event("side_effect_dispatching", entry.run_id, {"key": entry.key, "tool": entry.tool})
            return self._select(entry.key)

    def requeue(self, key):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            entry = self._select(key)
            if entry is None or entry.status is not EffectStatus.DISPATCHING:
                raise ValueError("only a dispatching entry can be requeued")
            self._set(key, status=EffectStatus.QUEUED.value)
            self._event("side_effect_requeued", entry.run_id, {"key": key, "tool": entry.tool})

    def finish(self, key, status, *, result=None, error=None):
        status = EffectStatus(status)
        if status not in TERMINAL:
            raise ValueError("finish requires done, failed or in_doubt")
        if status is EffectStatus.DONE and not isinstance(result, dict):
            raise ValueError("a done entry needs a JSON object result")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            entry = self._select(key)
            if entry is None or entry.status not in (EffectStatus.PENDING, EffectStatus.DISPATCHING):
                raise ValueError("only a pending or dispatching entry can finish")
            self._set(key, status=status.value, error=error,
                      result=None if result is None else canonical(result))
            self._event(f"side_effect_{status.value}", entry.run_id,
                        {"key": key, "tool": entry.tool, "error": error})
        return self._select(key)

    def entries(self, *, status=None, run_id=None):
        query = "SELECT key,run_id,fingerprint,tool,action,status,result,error,attempts FROM side_effects"
        clauses, params = [], []
        if status is not None:
            clauses.append("status=?")
            params.append(EffectStatus(status).value)
        if run_id is not None:
            clauses.append("run_id=?")
            params.append(run_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        return tuple(self._entry(r) for r in self.db.execute(query + " ORDER BY rowid", params).fetchall())

    def record(self, event_type, subject_id, payload):
        with self.db:
            self._event(event_type, subject_id, payload)
