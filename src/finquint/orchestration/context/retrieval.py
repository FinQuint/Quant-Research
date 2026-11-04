"""Retrieval: hard filters, then lexical rerank, then progressive fill (spec: "Selection").

1. Hard filters: permissions, current version only, the card's run and dataset versions, not
   deprecated. Working and provisional items are never retrieved (working comes from history;
   provisional is never evidence).
2. Progressive retrieval: one best item per sub-question first; then only sub-questions that
   still lack evidence get more; stop when none is missing, after max_rounds, or at max_items.
3. Duplicates (same topic and same content) are dropped. Items from the first pass are
   "required"; later items are "extra" and are the first to go when the token budget is short.

Strategies: "focused" prefers items whose topic matches the sub-question. "broad" (used after
a loop is detected) ranks on keywords only and takes more per round.

The current-version map and the keyword index are built once per retrieve() call.
"""
from collections import Counter
from dataclasses import dataclass, field
from hashlib import sha256
import math

from .evidence import words

RETRIEVABLE_TIERS = ("authoritative", "verified")
STRATEGIES = ("focused", "broad")


@dataclass(frozen=True)
class SubQuestion:
    topic: str
    query: str


@dataclass(frozen=True)
class Retrieved:
    evidence: object
    score: float
    sub_question: str      # topic it was retrieved for
    required: bool         # from the first pass: dropped last


@dataclass(frozen=True)
class Selection:
    items: tuple
    gaps: tuple                      # sub-question topics still without evidence
    rounds: int
    filtered: dict = field(default_factory=dict)   # reason -> count


def current_refs(items):
    """{evidence_id: ref of its current version}; items come in insertion order."""
    latest = {}
    for item in items:
        latest[item.evidence_id] = item.ref
    return latest


def ineligible_reason(evidence, *, store=None, run_id, dataset_versions, permissions, latest=None):
    """None if the item may be offered to this task, else why not. Pass latest (from
    current_refs) to avoid a store lookup per item."""
    if evidence.tier not in RETRIEVABLE_TIERS:
        return f"tier_{evidence.tier}"
    if not evidence.permissions <= frozenset(permissions):
        return "permission"
    if evidence.deprecated:
        return "deprecated"
    if latest is not None:
        current_ref = latest.get(evidence.evidence_id)
    else:
        current = store.current(evidence.evidence_id)
        current_ref = current.ref if current is not None else None
    if current_ref != evidence.ref:
        return "superseded_version"
    if evidence.run_id is not None and evidence.run_id != run_id:
        return "other_run"
    if evidence.dataset_name in dataset_versions and dataset_versions[evidence.dataset_name] != evidence.dataset_version:
        return "dataset_version"
    return None


class _Index:
    """TF-IDF keyword scoring over a fixed set of items (same weighting as the stores' search)."""

    def __init__(self, items):
        self.items = list(items)
        self.docs = {e.ref: Counter(words(f"{e.topic} {e.topic} {e.content}")) for e in self.items}
        self.df = Counter(t for counts in self.docs.values() for t in set(counts))

    def search(self, query):
        terms = words(query)
        scored = []
        for e in self.items:
            counts = self.docs[e.ref]
            score = sum(counts[t] * math.log(1 + len(self.items) / self.df[t]) for t in terms if counts[t])
            if score > 0:
                scored.append((round(score, 9), e))
        scored.sort(key=lambda pair: (-pair[0], pair[1].evidence_id, pair[1].version))
        return scored


class Retriever:
    def __init__(self, store, *, first_k=1, more_k=2, max_rounds=3, max_items=24, strategy="focused"):
        if strategy not in STRATEGIES:
            raise ValueError(f"strategy must be one of {STRATEGIES}")
        for name, value in (("first_k", first_k), ("more_k", more_k), ("max_rounds", max_rounds),
                            ("max_items", max_items)):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self.store = store
        self.first_k, self.more_k, self.max_rounds, self.max_items = first_k, more_k, max_rounds, max_items
        self.strategy = strategy

    def change_strategy(self):
        """After a repeated tool call with no state change: switch strategy."""
        self.strategy = "broad" if self.strategy == "focused" else "focused"
        return self.strategy

    def _ranked(self, sub_question, index):
        hits = index.search(f"{sub_question.topic} {sub_question.query}")
        if self.strategy == "focused":
            hits.sort(key=lambda pair: (pair[1].topic != sub_question.topic, -pair[0],
                                        pair[1].evidence_id, pair[1].version))
        return hits

    def retrieve(self, sub_questions, *, run_id, dataset_versions=None, permissions=frozenset()):
        dataset_versions = dict(dataset_versions or {})
        items = self.store.items()
        latest = current_refs(items)
        filtered, eligible = {}, []
        for item in items:
            reason = ineligible_reason(item, run_id=run_id, dataset_versions=dataset_versions,
                                       permissions=permissions, latest=latest)
            if reason:
                filtered[reason] = filtered.get(reason, 0) + 1
            else:
                eligible.append(item)
        index = _Index(eligible)
        chosen, refs, contents = [], set(), set()
        covered = set()
        pending = list(sub_questions)
        rounds = 0
        while pending and rounds < self.max_rounds and len(chosen) < self.max_items:
            rounds += 1
            take = self.first_k if rounds == 1 else self.more_k
            if self.strategy == "broad":
                take += self.more_k
            for question in pending:
                added = 0
                for score, item in self._ranked(question, index):
                    if added >= take or len(chosen) >= self.max_items:
                        break
                    duplicate = (item.topic, sha256(item.content.encode()).hexdigest())
                    if item.ref in refs or duplicate in contents:
                        continue
                    refs.add(item.ref)
                    contents.add(duplicate)
                    chosen.append(Retrieved(item, score, question.topic, required=rounds == 1))
                    added += 1
                    if item.topic == question.topic:
                        covered.add(question.topic)
            pending = [q for q in pending if q.topic not in covered]
        gaps = tuple(q.topic for q in sub_questions if q.topic not in covered)
        return Selection(tuple(chosen), gaps, rounds, dict(sorted(filtered.items())))
