"""Health triggers: deterministic checks on each request and each model output (spec table).

    Signal                                   Action
    utilization over threshold               drop old tool outputs, then rebuild
    output cites a missing ID / version      reject output, rebuild evidence
    output contradicts the task card         rebuild from task card
      (wrong run or code SHA, criteria forgotten or invented)
    same tool call, no state change          stop loop, change retrieval strategy
    mixed run IDs or dataset versions        reset working context
    evidence conflict (same claim, two values)   ESCALATE (Phase 5 addition)
    failed rebuilds reach the limit          ESCALATE
"""
from dataclasses import dataclass
from enum import Enum

from ..verify.citations import check_citations


class Signal(str, Enum):
    HIGH_UTILIZATION = "high_utilization"
    MISSING_CITATION = "missing_citation"
    CONTRADICTS_CARD = "contradicts_card"
    REPEATED_TOOL_CALL = "repeated_tool_call"
    MIXED_VERSIONS = "mixed_versions"
    EVIDENCE_CONFLICT = "evidence_conflict"
    REBUILDS_EXHAUSTED = "rebuilds_exhausted"


class Action(str, Enum):
    CONTINUE = "continue"
    DROP_OLD_OUTPUTS_AND_REBUILD = "drop_old_outputs_and_rebuild"
    REBUILD_EVIDENCE = "rebuild_evidence"
    REBUILD_FROM_CARD = "rebuild_from_card"
    CHANGE_RETRIEVAL = "change_retrieval"
    RESET_WORKING = "reset_working"
    ESCALATE = "escalate"


ACTION_FOR = {
    Signal.HIGH_UTILIZATION: Action.DROP_OLD_OUTPUTS_AND_REBUILD,
    Signal.MISSING_CITATION: Action.REBUILD_EVIDENCE,
    Signal.CONTRADICTS_CARD: Action.REBUILD_FROM_CARD,
    Signal.REPEATED_TOOL_CALL: Action.CHANGE_RETRIEVAL,
    Signal.MIXED_VERSIONS: Action.RESET_WORKING,
    Signal.EVIDENCE_CONFLICT: Action.ESCALATE,
    Signal.REBUILDS_EXHAUSTED: Action.ESCALATE,
}
# When several signals fire, the most severe action wins.
SEVERITY = (Action.ESCALATE, Action.RESET_WORKING, Action.REBUILD_FROM_CARD, Action.REBUILD_EVIDENCE,
            Action.DROP_OLD_OUTPUTS_AND_REBUILD, Action.CHANGE_RETRIEVAL, Action.CONTINUE)


@dataclass(frozen=True)
class Finding:
    signal: Signal
    detail: str


@dataclass(frozen=True)
class HealthReport:
    findings: tuple = ()

    @property
    def ok(self):
        return not self.findings

    @property
    def action(self):
        actions = {ACTION_FOR[f.signal] for f in self.findings}
        return next((a for a in SEVERITY if a in actions), Action.CONTINUE)

    @property
    def signals(self):
        return tuple(dict.fromkeys(f.signal for f in self.findings))


def _items(built):
    items = list(built.evidence)
    items += [h.evidence for h in built.history if h.kind == "tool_output"]
    return items


def check_request(built, card, *, utilization_threshold):
    findings = []
    if built.utilization > utilization_threshold:
        findings.append(Finding(Signal.HIGH_UTILIZATION,
                                f"utilization {built.utilization:.2f} over {utilization_threshold:.2f}"))
    for item in _items(built):
        if item.run_id is not None and item.run_id != card.run_id:
            findings.append(Finding(Signal.MIXED_VERSIONS, f"{item.evidence_id} is from run {item.run_id}"))
        name = item.dataset_name
        if name in card.dataset_versions and card.dataset_versions[name] != item.dataset_version:
            findings.append(Finding(Signal.MIXED_VERSIONS, f"{item.evidence_id} uses {item.dataset}, card pins "
                                                           f"{name}@{card.dataset_versions[name]}"))
    # Conflicts are checked over everything considered, not only what fit the budget, so a
    # trimmed request cannot hide a disagreement.
    considered = list(built.candidates) + [i for i in _items(built) if i not in built.candidates]
    findings += [Finding(Signal.EVIDENCE_CONFLICT, detail) for detail in evidence_conflicts(considered)]
    return HealthReport(tuple(findings))


def evidence_conflicts(items):
    """Pairs of items that state different values for the same claim key."""
    seen, conflicts = {}, []
    for item in items:
        for key, value in sorted(item.claims.items()):
            first = seen.setdefault(key, (item.evidence_id, value))
            if first[1] != value and first[0] != item.evidence_id:
                conflicts.append(f"{key}: {first[0]} says {first[1]!r}, {item.evidence_id} says {value!r}")
    return conflicts


def check_output(output, built, card, *, store):
    findings = []
    if output.get("run_id") != card.run_id:
        findings.append(Finding(Signal.CONTRADICTS_CARD, f"run_id {output.get('run_id')!r} is not {card.run_id!r}"))
    if output.get("code_sha") not in (None, card.code_sha):
        findings.append(Finding(Signal.CONTRADICTS_CARD, f"code_sha {output.get('code_sha')!r} is not {card.code_sha!r}"))
    addressed = [c.get("id") for c in output.get("criteria", [])]
    missing = [cid for cid in card.criterion_ids if cid not in addressed]
    unknown = [cid for cid in addressed if cid not in card.criterion_ids]
    if missing:
        findings.append(Finding(Signal.CONTRADICTS_CARD, f"criteria not addressed: {', '.join(missing)}"))
    if unknown:
        findings.append(Finding(Signal.CONTRADICTS_CARD, f"criteria not on the card: {', '.join(map(str, unknown))}"))
    for criterion in output.get("criteria", []):
        citations = criterion.get("citations", [])
        if not citations:
            findings.append(Finding(Signal.MISSING_CITATION, f"{criterion.get('id')}: no citation"))
        for reason in check_citations(citations, store=store, run_id=card.run_id, offered=built.offered):
            findings.append(Finding(Signal.MISSING_CITATION, f"{criterion.get('id')}: {reason}"))
    return HealthReport(tuple(findings))


class LoopDetector:
    """Same tool call (fingerprint) seen again with an unchanged state hash -> repeated."""

    def __init__(self):
        self._seen = set()

    def observe(self, fingerprint, state_hash):
        key = (fingerprint, state_hash)
        if key in self._seen:
            return Finding(Signal.REPEATED_TOOL_CALL, f"tool call {fingerprint[:12]} repeated with no state change")
        self._seen.add(key)
        return None
