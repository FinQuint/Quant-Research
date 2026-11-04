"""Typed handoff records between agents: JSON payloads, never transcripts (spec: request layout 3)."""
from dataclasses import dataclass, field
import json

from finquint.autonomous.models import canonical, identifier

from .tags import handoff_line

HANDOFF_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Handoff:
    source: str              # agent that produced it
    target: str              # agent it is for
    run_id: str
    task_id: str
    kind: str                # e.g. "research_findings", "analysis_result"
    payload: dict
    evidence_ids: tuple = ()
    schema_version: int = field(default=HANDOFF_SCHEMA_VERSION)

    def __post_init__(self):
        for value in (self.source, self.target, self.run_id, self.task_id, self.kind):
            identifier(value)
        if not isinstance(self.payload, dict):
            raise ValueError("a handoff payload must be a JSON object")
        try:
            canonical(self.payload)   # JSON only
        except (TypeError, ValueError) as exc:
            raise ValueError(f"a handoff payload must be plain JSON: {exc}") from exc
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        if self.schema_version != HANDOFF_SCHEMA_VERSION:
            raise ValueError(f"unsupported handoff schema version {self.schema_version}")

    def render(self):
        body = {"kind": self.kind, "run_id": self.run_id, "task_id": self.task_id, "payload": self.payload,
                "evidence_ids": list(self.evidence_ids)}
        return handoff_line(self.source, self.target, json.dumps(body, sort_keys=True, separators=(",", ":")))
