"""Evidence items and the store they are retrieved from.

Each item is one version of one piece of evidence. The store keeps every version (so past
runs stay reproducible) and knows which one is current. Retrieval is lexical (keyword)
scoring behind the EvidenceStore interface; an embedding or Postgres backend can replace it
later without touching the builder.
"""
from collections import Counter
from dataclasses import dataclass, field, replace
import json
import math
import re
import threading
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from finquint.autonomous.models import identifier

from .tags import evidence_line

TIERS = ("authoritative", "verified", "provisional", "working")
_WORD = re.compile(r"[a-z0-9]+")


def words(text):
    return _WORD.findall(str(text).lower())


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    version: str
    topic: str
    content: str
    run_id: str | None = None          # None: not tied to a run (docs, source at SHA)
    dataset: str | None = None         # "name@version" the evidence was computed on
    tier: str = "authoritative"
    permissions: frozenset = frozenset()   # all must be granted to read it
    deprecated: bool = False
    claims: dict = field(default_factory=dict)   # structured facts, e.g. {"compounding": "nominal"}
    source: str = ""

    def __post_init__(self):
        identifier(self.evidence_id)
        identifier(self.topic)
        if not isinstance(self.version, str) or not self.version.strip() or " " in self.version:
            raise ValueError("evidence version must be a non-empty string without spaces")
        if self.tier not in TIERS:
            raise ValueError(f"tier must be one of {TIERS}")
        if self.dataset is not None and "@" not in self.dataset:
            raise ValueError("dataset must look like name@version")
        object.__setattr__(self, "permissions", frozenset(self.permissions))
        object.__setattr__(self, "claims", MappingProxyType({str(k): str(v) for k, v in dict(self.claims).items()}))
        # Fail now, not mid-run: IDs, version, run and dataset must fit the request tag format.
        evidence_line(self.evidence_id, self.version, self.run_id, self.topic, "", self.dataset)

    @property
    def ref(self):
        return (self.evidence_id, self.version)

    @property
    def dataset_name(self):
        return self.dataset.split("@", 1)[0] if self.dataset else None

    @property
    def dataset_version(self):
        return self.dataset.split("@", 1)[1] if self.dataset else None

    def to_json(self):
        data = {name: getattr(self, name) for name in self.__dataclass_fields__}
        data["permissions"] = sorted(self.permissions)
        data["claims"] = dict(self.claims)
        return json.dumps(data, sort_keys=True)

    @classmethod
    def from_json(cls, text):
        data = json.loads(text)
        data["permissions"] = frozenset(data["permissions"])
        return cls(**data)


@runtime_checkable
class EvidenceStore(Protocol):
    def put(self, evidence: Evidence) -> None: ...
    def get(self, evidence_id: str, version: str | None = None) -> Evidence | None: ...
    def current(self, evidence_id: str) -> Evidence | None: ...
    def items(self) -> list: ...


class _Lexical:
    """Keyword scoring shared by the stores: term frequency x inverse document frequency."""

    def search(self, query, *, limit=None, current_only=True):
        items = self.items()
        if current_only:
            latest = {}
            for e in items:          # items() is in insertion order, so the last version wins
                latest[e.evidence_id] = e.ref
            items = [e for e in items if latest[e.evidence_id] == e.ref]
        terms = words(query)
        if not terms or not items:
            return []
        docs = {e.ref: Counter(words(f"{e.topic} {e.topic} {e.content}")) for e in items}
        df = Counter(t for counts in docs.values() for t in set(counts))
        scored = []
        for e in items:
            counts = docs[e.ref]
            score = sum(counts[t] * math.log(1 + len(items) / df[t]) for t in terms if counts[t])
            if score > 0:
                scored.append((round(score, 9), e))
        scored.sort(key=lambda pair: (-pair[0], pair[1].evidence_id, pair[1].version))
        return scored[:limit] if limit else scored


class InMemoryEvidenceStore(_Lexical):
    def __init__(self, items=()):
        self._items = {}
        self._order = {}
        self._lock = threading.Lock()
        for item in items:
            self.put(item)

    def put(self, evidence):
        with self._lock:
            if evidence.ref not in self._order:
                self._order[evidence.ref] = len(self._order)
            self._items[evidence.ref] = evidence

    def get(self, evidence_id, version=None):
        if version is None:
            return self.current(evidence_id)
        return self._items.get((evidence_id, version))

    def current(self, evidence_id):
        versions = [ref for ref in self._order if ref[0] == evidence_id]
        return self._items[max(versions, key=self._order.get)] if versions else None

    def items(self):
        return [self._items[ref] for ref in sorted(self._order, key=self._order.get)]

    def deprecate(self, evidence_id):
        for ref in [r for r in self._order if r[0] == evidence_id]:
            self.put(replace(self._items[ref], deprecated=True))


class SQLiteEvidenceStore(_Lexical):
    """Evidence in the run database (shares the connection with ResearchStore)."""

    def __init__(self, connection):
        self.db = connection
        self.db.execute("""CREATE TABLE IF NOT EXISTS context_evidence (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, evidence_id TEXT NOT NULL, version TEXT NOT NULL,
            data TEXT NOT NULL, UNIQUE (evidence_id, version))""")
        self.db.commit()

    def put(self, evidence):
        with self.db:
            self.db.execute("""INSERT INTO context_evidence (evidence_id, version, data) VALUES (?, ?, ?)
                ON CONFLICT (evidence_id, version) DO UPDATE SET data = excluded.data""",
                            (evidence.evidence_id, evidence.version, evidence.to_json()))

    def get(self, evidence_id, version=None):
        if version is None:
            return self.current(evidence_id)
        row = self.db.execute("SELECT data FROM context_evidence WHERE evidence_id = ? AND version = ?",
                              (evidence_id, version)).fetchone()
        return Evidence.from_json(row[0]) if row else None

    def current(self, evidence_id):
        row = self.db.execute("SELECT data FROM context_evidence WHERE evidence_id = ? ORDER BY seq DESC LIMIT 1",
                              (evidence_id,)).fetchone()
        return Evidence.from_json(row[0]) if row else None

    def items(self):
        return [Evidence.from_json(row[0]) for row in
                self.db.execute("SELECT data FROM context_evidence ORDER BY seq")]

    def deprecate(self, evidence_id):
        for item in [e for e in self.items() if e.evidence_id == evidence_id]:
            self.put(replace(item, deprecated=True))
