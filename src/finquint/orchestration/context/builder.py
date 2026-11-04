"""Request builder: the spec's request layout and budgets.

Layout (stable first, volatile last, for prompt caching):
  1. system prompt and tool schemas      stable
  2. task card                           stable, verbatim, never dropped
  3. handoff records
  4. retrieved evidence, tagged with ID and version
  5. recent working history (last N entries)
  6. volatile: budget left, the instruction
  then the output reserve (max_output_tokens).

Budget fill order when space is short: card, handoffs and instruction must fit (else
ContextOverflow); then required evidence; then history (results and notes before call logs, newest first); then extra evidence.
A rebuild (minimal=True) uses only the card, handoffs, required evidence and history since the
last verified checkpoint (spec: "Rebuild means ...").
"""
from dataclasses import dataclass, field
import json
import math
from pathlib import Path

from ..models.socket import ModelRequest, Segment, SegmentRole
from .retrieval import Retriever, SubQuestion
from .tags import REPORT_SCHEMA, evidence_line, mention_line

CONFIG_PATH = Path(__file__).resolve().parents[4] / "configs" / "context_policy.json"
HISTORY_KINDS = ("tool_call", "tool_output", "note")


class ContextOverflow(Exception):
    """Even the pinned parts (card, handoffs, instruction) do not fit: escalate."""


@dataclass(frozen=True)
class ContextPolicy:
    """Every number here is (tune); configs/context_policy.json holds the values picked by the
    Phase 5 sweep (evals/context)."""
    output_reserve: float = 0.15          # share of the window kept for the reply
    utilization_threshold: float = 0.70   # spec: utilization over 70% -> drop old outputs, rebuild
    history_last_n: int = 8               # spec: last N tool calls
    max_failed_rebuilds: int = 2          # spec: two failed rebuilds in a row -> ESCALATE
    retrieval_first_k: int = 1
    retrieval_more_k: int = 2
    retrieval_max_rounds: int = 3
    retrieval_max_items: int = 24

    def __post_init__(self):
        if not 0 < self.output_reserve < 1 or not 0 < self.utilization_threshold <= 1:
            raise ValueError("output_reserve and utilization_threshold must be fractions")
        for name in ("history_last_n", "max_failed_rebuilds", "retrieval_first_k", "retrieval_more_k",
                     "retrieval_max_rounds", "retrieval_max_items"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")

    @classmethod
    def load(cls, path=CONFIG_PATH):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(**data["policy"])

    def to_dict(self):
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    def retriever(self, store, strategy="focused"):
        return Retriever(store, first_k=self.retrieval_first_k, more_k=self.retrieval_more_k,
                         max_rounds=self.retrieval_max_rounds, max_items=self.retrieval_max_items,
                         strategy=strategy)


@dataclass(frozen=True)
class HistoryEntry:
    """One item of working history.

    tool_output: carries the Evidence it produced (rendered as an evidence line).
    note: an agent's own note; mentions are (evidence_id, version, topic) it refers to.
    tool_call: fingerprint and state_hash feed loop detection.
    """
    step: int
    kind: str
    content: str = ""
    run_id: str | None = None
    evidence: object = None
    mentions: tuple = ()
    fingerprint: str = ""
    state_hash: str = ""

    def __post_init__(self):
        if self.kind not in HISTORY_KINDS:
            raise ValueError(f"history kind must be one of {HISTORY_KINDS}")
        if self.kind == "tool_output" and self.evidence is None:
            raise ValueError("a tool_output entry carries its Evidence")
        object.__setattr__(self, "mentions", tuple(tuple(m) for m in self.mentions))

    def render(self):
        if self.kind == "tool_output":
            e = self.evidence
            return evidence_line(e.evidence_id, e.version, e.run_id, e.topic, e.content, e.dataset)
        if self.kind == "note":
            refs = [mention_line(i, v, t, self.content) for i, v, t in self.mentions]
            return "\n".join(refs) if refs else f"Note: {' '.join(self.content.split())}"
        return f"Tool call (step {self.step}): {' '.join(self.content.split())}"


@dataclass(frozen=True)
class BuiltContext:
    request: ModelRequest
    input_tokens: int
    input_budget: int
    evidence: tuple            # Evidence items included
    history: tuple             # HistoryEntry items included
    gaps: tuple                # sub-question topics with no evidence
    dropped: dict = field(default_factory=dict)   # what did not fit: {"evidence": n, "history": n}
    minimal: bool = False
    candidates: tuple = ()     # every evidence item considered, before the budget trimmed any
    handoff_refs: frozenset = frozenset()   # current versions of evidence IDs named in handoffs

    @property
    def utilization(self):
        return self.input_tokens / self.input_budget

    @property
    def offered(self):
        """(evidence_id, version) of every evidence item the model was shown."""
        refs = {e.ref for e in self.evidence}
        refs |= {h.evidence.ref for h in self.history if h.kind == "tool_output"}
        return refs | self.handoff_refs


def stale_output(item, store, permissions):
    """History gets the same version and permission filters as retrieval. Other-run and
    dataset mismatches are left in, so the mixed-versions health trigger sees and resets them."""
    current = store.current(item.evidence_id)
    return (item.deprecated or (current is not None and (current.ref != item.ref or current.deprecated))
            or not item.permissions <= frozenset(permissions))


def render_evidence(item):
    return evidence_line(item.evidence_id, item.version, item.run_id, item.topic, item.content, item.dataset)


class ContextBuilder:
    def __init__(self, policy, *, counter, window_tokens):
        if type(window_tokens) is not int or window_tokens < 16:
            raise ValueError("window_tokens must be an integer of at least 16")
        self.policy = policy
        self.counter = counter
        self.window = window_tokens

    @property
    def input_budget(self):
        return math.floor(self.window * (1 - self.policy.output_reserve))

    def _tokens(self, text):
        return self.counter.count(text + "\n\n") if text else 0

    def build(self, card, *, retriever, system, instruction, handoffs=(), history=(), sub_questions=None,
              permissions=frozenset(), output_schema=REPORT_SCHEMA, budget_left=None, minimal=False,
              since_checkpoint=0, limit=None):
        """limit: fill only up to this share of the input budget (a rebuild aims under the
        utilization threshold). The pinned parts may still use the full budget."""
        budget = self.input_budget
        fill_to = budget if limit is None else min(budget, math.floor(budget * limit))
        sub_questions = tuple(sub_questions if sub_questions is not None else
                              (SubQuestion(c.topic, c.text) for c in card.criteria))
        volatile = instruction if not budget_left else (
            instruction + "\nBudget left: " + ", ".join(f"{k}={v}" for k, v in sorted(budget_left.items())))
        fixed = [Segment(SegmentRole.SYSTEM, system, stable=True),
                 Segment(SegmentRole.TASK_CARD, card.render(), stable=True, ids=card.criterion_ids)]
        fixed += [Segment(SegmentRole.HANDOFF, h.render(), ids=h.evidence_ids) for h in handoffs]
        used = sum(self._tokens(s.content) for s in fixed) + self._tokens(volatile)
        if used > budget:
            raise ContextOverflow(f"task card, handoffs and instruction need {used} tokens; budget is {budget}")

        selection = retriever.retrieve(sub_questions, run_id=card.run_id,
                                       dataset_versions=dict(card.dataset_versions), permissions=permissions)
        required = [r.evidence for r in selection.items if r.required]
        extra = [] if minimal else [r.evidence for r in selection.items if not r.required]
        window = list(history)[since_checkpoint:] if minimal else list(history)
        recent = window[-self.policy.history_last_n:]
        stale = [h for h in recent if h.kind == "tool_output" and stale_output(h.evidence, retriever.store, permissions)]
        recent = [h for h in recent if h not in stale]
        candidates = tuple(r.evidence for r in selection.items) + tuple(
            h.evidence for h in recent if h.kind == "tool_output")
        dropped = {"evidence": 0, "history": len(window) - len(recent) - len(stale), "stale_history": len(stale)}

        evidence = []
        for item in required:
            cost = self._tokens(render_evidence(item))
            if used + cost <= fill_to:
                evidence.append(item)
                used += cost
            else:
                dropped["evidence"] += 1
        # A history output whose evidence is already included is not repeated.
        included = {e.ref for e in evidence}
        recent = [h for h in recent if not (h.kind == "tool_output" and h.evidence.ref in included)]
        # Results before call logs, newest first within each; then back to chronological order.
        ranked = ([e for e in reversed(recent) if e.kind != "tool_call"]
                  + [e for e in reversed(recent) if e.kind == "tool_call"])
        kept_history = []
        for entry in ranked:
            cost = self._tokens(entry.render())
            if used + cost <= fill_to:
                kept_history.append(entry)
                used += cost
            else:
                dropped["history"] += 1
        order = {id(e): i for i, e in enumerate(recent)}
        kept_history.sort(key=lambda e: order[id(e)])
        in_history = {h.evidence.ref for h in kept_history if h.kind == "tool_output"}
        for item in extra:
            if item.ref in in_history:
                continue
            cost = self._tokens(render_evidence(item))
            if used + cost <= fill_to:
                evidence.append(item)
                used += cost
            else:
                dropped["evidence"] += 1
        handoff_refs = set()
        for handoff in handoffs:
            for evidence_id in handoff.evidence_ids:
                current = retriever.store.current(evidence_id)
                if current is not None:
                    handoff_refs.add(current.ref)

        segments = list(fixed)
        if evidence:
            segments.append(Segment(SegmentRole.EVIDENCE, "\n".join(render_evidence(e) for e in evidence),
                                    ids=tuple(e.evidence_id for e in evidence)))
        if kept_history:
            segments.append(Segment(SegmentRole.HISTORY, "\n".join(h.render() for h in kept_history)))
        segments.append(Segment(SegmentRole.VOLATILE, volatile))
        request = ModelRequest(card.run_id, card.task_id, tuple(segments), output_schema,
                               max_output_tokens=self.window - budget)
        tokens = self.counter.count(request.text())
        return BuiltContext(request, tokens, budget, tuple(evidence), tuple(kept_history), selection.gaps,
                            dropped, minimal, candidates, frozenset(handoff_refs))
