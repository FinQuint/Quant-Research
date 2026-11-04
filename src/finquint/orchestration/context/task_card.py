"""Task card: the immutable record of what a task must achieve (spec: "Context and knowledge layer").

It is injected verbatim on every model call, so it is never summarized and never dropped.
It holds budget limits; the budget left changes every call and goes in the request's volatile
part instead, so the card stays byte-identical (and cacheable) for the whole task.
"""
from dataclasses import dataclass, field
from hashlib import sha256
import json
from types import MappingProxyType

from finquint.autonomous.models import identifier

from .tags import _clean, _one_line, card_line, criterion_line


@dataclass(frozen=True)
class AcceptanceCriterion:
    criterion_id: str
    text: str
    topic: str       # which evidence answers it; drives retrieval sub-questions

    def __post_init__(self):
        identifier(self.criterion_id)
        identifier(self.topic)
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("a criterion needs text")


@dataclass(frozen=True)
class TaskCard:
    run_id: str
    task_id: str
    goal: str
    criteria: tuple
    code_sha: str
    dataset_versions: dict = field(default_factory=dict)
    constraints: tuple = ()
    budget_limits: dict = field(default_factory=dict)   # e.g. {"tool_calls": 60, "model_calls": 10}

    def __post_init__(self):
        identifier(self.run_id)
        identifier(self.task_id)
        if not self.goal.strip() or not self.code_sha.strip():
            raise ValueError("a task card needs a goal and a code_sha")
        criteria = tuple(self.criteria)
        if not criteria or not all(isinstance(c, AcceptanceCriterion) for c in criteria):
            raise ValueError("a task card needs AcceptanceCriterion items")
        if len({c.criterion_id for c in criteria}) != len(criteria):
            raise ValueError("criterion IDs must be unique")
        object.__setattr__(self, "criteria", criteria)
        object.__setattr__(self, "constraints", tuple(self.constraints))
        object.__setattr__(self, "dataset_versions", MappingProxyType(dict(self.dataset_versions)))
        object.__setattr__(self, "budget_limits", MappingProxyType(dict(self.budget_limits)))
        self.render()   # fail now, not mid-run: every value must fit the request tag format

    @property
    def criterion_ids(self):
        return tuple(c.criterion_id for c in self.criteria)

    @property
    def topics(self):
        return tuple(dict.fromkeys(c.topic for c in self.criteria))

    def render(self):
        """The card as request text. Deterministic: the same card always renders the same bytes."""
        lines = [card_line(self.run_id, self.task_id, self.code_sha, dict(self.dataset_versions), self.goal)]
        lines += [criterion_line(c.criterion_id, c.topic, c.text) for c in self.criteria]
        lines += [f"Constraint: {_one_line(c)}" for c in self.constraints]   # one line: no injected tags
        if self.budget_limits:
            lines.append("Budget limits: " + ", ".join(f"{_clean(k)}={_clean(v)}"
                                                      for k, v in sorted(self.budget_limits.items())))
        return "\n".join(lines)

    def to_dict(self):
        return {"run_id": self.run_id, "task_id": self.task_id, "goal": self.goal, "code_sha": self.code_sha,
                "criteria": [[c.criterion_id, c.topic, c.text] for c in self.criteria],
                "dataset_versions": dict(sorted(self.dataset_versions.items())),
                "constraints": list(self.constraints), "budget_limits": dict(sorted(self.budget_limits.items()))}

    def digest(self):
        return sha256(json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
