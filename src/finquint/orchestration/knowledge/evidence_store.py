"""Tiers, promotion and provenance (spec: "Memory tiers").

An agent cannot promote provisional knowledge by writing a summary. promote() is a deterministic
verifier pass: the claim is promoted to the verified tier only when

  * every placeholder in its template renders from the MetricStore,
  * every other number in it traces (verify.numbers.check_numerals), and
  * it cites at least one item, and every cited item is current, not deprecated, from this run
    (or run-independent) and in the authoritative or verified tier.

The verified item records what it rests on (Evidence.supports). demote_superseded() demotes a
verified item to provisional, as a new version, as soon as anything it rests on is superseded
or deprecated. Old versions are kept, so past runs stay reproducible.
"""
from dataclasses import dataclass, field, replace

from finquint.autonomous.models import identifier

from ..context.evidence import Evidence, current_items
from ..verify.citations import check_citation
from ..verify.numbers import RenderError, check_numerals, render

TRUSTED_TIERS = ("authoritative", "verified")


@dataclass(frozen=True)
class Claim:
    claim_id: str
    version: str
    topic: str
    template: str            # text with {{metric:...}} placeholders
    citations: tuple         # (evidence_id, version) the claim rests on
    run_id: str | None = None
    claims: dict = field(default_factory=dict)

    def __post_init__(self):
        identifier(self.claim_id)
        object.__setattr__(self, "citations", tuple((str(i), str(v)) for i, v in self.citations))


@dataclass(frozen=True)
class Promotion:
    promoted: bool
    reasons: tuple = ()
    evidence: Evidence = None


def promote(claim, *, store, metrics, identifiers=()):
    if store.get(claim.claim_id, f"{claim.version}.verified") is not None:
        return Promotion(False, (f"{claim.claim_id} v{claim.version} was already promoted; "
                                 "a changed claim needs a new version",))
    reasons = []
    try:
        rendered = render(claim.template, metrics)
    except RenderError as exc:
        return Promotion(False, exc.errors)
    allowed = set(identifiers) | {i for i, _ in claim.citations} | {f"v{v}" for _, v in claim.citations}
    reasons += [f"untraced number {u.text!r}" for u in check_numerals(rendered, identifiers=allowed)]
    if not claim.citations:
        reasons.append("a claim must cite at least one authoritative or verified item")
    for evidence_id, version in claim.citations:
        failure = check_citation(evidence_id, version, store=store, run_id=claim.run_id)
        if failure:
            reasons.append(failure)
            continue
        if store.get(evidence_id, version).tier not in TRUSTED_TIERS:
            reasons.append(f"{evidence_id} v{version}: tier {store.get(evidence_id, version).tier} is not trusted")
    if reasons:
        return Promotion(False, tuple(reasons))
    item = Evidence(claim.claim_id, f"{claim.version}.verified", claim.topic, rendered.text, run_id=claim.run_id,
                    tier="verified", claims=dict(claim.claims), source=f"promoted:{claim.claim_id}",
                    supports=claim.citations)
    store.put(item)
    return Promotion(True, (), item)


def demote_superseded(store):
    """Demote every current verified item whose support is superseded or deprecated.
    Returns the refs of the new provisional versions."""
    latest = {item.evidence_id: item for item in current_items(store)}
    demoted = []
    for item in list(latest.values()):
        if item.tier != "verified" or not item.supports:
            continue
        broken = []
        for evidence_id, version in item.supports:
            current = latest.get(evidence_id)
            if current is None or current.ref != (evidence_id, version) or current.deprecated:
                broken.append(f"{evidence_id} v{version}")
        if broken:
            new = replace(item, version=f"{item.version}.demoted", tier="provisional",
                          source=f"{item.source}; demoted: {', '.join(broken)} changed")
            store.put(new)
            demoted.append(new.ref)
    return demoted
