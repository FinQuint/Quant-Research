"""Requests and output contracts for the two recovery agents (ADR-020).

Repair agent: sees the task card and a failure packet (the failed call, its recovery rule, the
verbatim failure, and every earlier attempt with its reflection and outcome). Replies with a
structured reflection and a revised call, or asks to escalate.

Validator agent: sees the task card and a review packet (the original call and failure, the final
call, the changed parameters, the output and the gate results). It never sees the repair agent's
reflections or messages (blind review). Replies with a verdict and one finding per criterion.
"""
from hashlib import sha256
import json

from finquint.autonomous.models import canonical

from ..context.task_card import AcceptanceCriterion, TaskCard
from ..models.socket import ModelRequest, Segment, SegmentRole

REPAIR_SCHEMA = {
    "type": "object",
    "required": ["reflection"],
    "additionalProperties": False,
    "properties": {
        "reflection": {
            "type": "object",
            "required": ["diagnosis", "evidence", "hypothesis", "changes", "differs_from_previous"],
            "additionalProperties": False,
            "properties": {
                "diagnosis": {"type": "string", "minLength": 1},
                "evidence": {"type": "string", "minLength": 1,
                             "description": "a passage copied exactly from the failure text in the packet"},
                "hypothesis": {"type": "string", "minLength": 1},
                "changes": {"type": "array", "items": {"type": "string", "minLength": 1},
                            "description": "the parameter paths you change, plus \"method\" if you swap the method"},
                "differs_from_previous": {"type": "string", "minLength": 1},
            }},
        "proposal": {
            "type": "object",
            "required": ["method", "parameters"],
            "additionalProperties": False,
            "properties": {"method": {"type": "string", "minLength": 1}, "parameters": {"type": "object"}}},
        "escalate": {"type": "string", "description": "set only if no allowed change can fix the failure"},
    },
}

REPAIR_SYSTEM = """You are the repair agent in a governed quant research pipeline. A deterministic method call failed its checks. Propose one revised call that could pass them without changing the question the task asks.

Rules (checked by code; a proposal that breaks one is rejected and counts as an attempt):
- Use the original method or one listed under rule.alternatives. Never invent a method.
- Change only parameters listed under rule.tunable, within their bounds. Every other parameter, including rule.frozen, must stay exactly as in original_call.
- Do not repeat the original call or any earlier attempt.
- reflection.evidence must be copied exactly from the failure text in the packet.
- reflection.changes must list exactly the parameter paths you change (dotted for nested objects), plus "method" if you change the method.
- If no allowed change can fix the failure, reply with reflection and "escalate" (a reason) instead of a proposal.

Reply with only JSON matching the output schema."""

VALIDATOR_SCHEMA = {
    "type": "object",
    "required": ["verdict", "reasons", "criteria"],
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["accept", "reject", "escalate"]},
        "reasons": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
        "criteria": {"type": "array", "items": {
            "type": "object", "required": ["id", "satisfied", "finding"], "additionalProperties": False,
            "properties": {"id": {"type": "string", "minLength": 1}, "satisfied": {"type": "boolean"},
                           "finding": {"type": "string", "minLength": 1}}}},
    },
}

VALIDATOR_SYSTEM = """You are the independent validator in a governed quant research pipeline. Deterministic checks have already passed. Decide whether the result still answers the task as stated on the task card.

In "repair" mode a failed call was revised by another agent; you see what changed but not its reasoning. Look for goal drift: a change that makes the checks pass by answering a different question (a different horizon, confidence level, sample, universe or definition than the task asks for).
In "sample" mode you audit a result produced with no repair.

Give one finding per acceptance criterion on the card (its ID, whether it is satisfied, and why). verdict: "accept" only if every criterion is satisfied; "reject" if the result answers a different question or fails a criterion; "escalate" if you cannot tell from what you were given.

Reply with only JSON matching the output schema."""


def default_card(state):
    """A card for runs submitted without one: the objective is the single criterion."""
    plan = state["plan"]
    return TaskCard(run_id=plan["run_id"], task_id="run", goal=plan["objective"], code_sha=plan["code_version"],
                    criteria=(AcceptanceCriterion("objective", "The result answers the stated objective without "
                                                  "changing the question it asks.", "objective"),))


def _json_block(title, value):
    return f"{title} (JSON):\n" + json.dumps(value, sort_keys=True, indent=1)


def repair_request(card, packet, *, attempt, max_attempts, budget_left_usd, task_id):
    volatile = (f"Attempt {attempt} of {max_attempts}. Budget left for this recovery: "
                f"${budget_left_usd:.2f}. Propose one revised call.")
    return ModelRequest(card.run_id, f"{task_id}-repair-{attempt}", (
        Segment(SegmentRole.SYSTEM, REPAIR_SYSTEM, stable=True),
        Segment(SegmentRole.TASK_CARD, card.render(), stable=True, ids=card.criterion_ids),
        Segment(SegmentRole.HANDOFF, _json_block("Failure packet", packet)),
        Segment(SegmentRole.VOLATILE, volatile)), REPAIR_SCHEMA, max_output_tokens=1024)


def validator_request(card, review_packet, *, request_run_id, task_id):
    return ModelRequest(request_run_id, f"{task_id}-validate", (
        Segment(SegmentRole.SYSTEM, VALIDATOR_SYSTEM, stable=True),
        Segment(SegmentRole.TASK_CARD, card.render(), stable=True, ids=card.criterion_ids),
        Segment(SegmentRole.HANDOFF, _json_block("Review packet", review_packet)),
        Segment(SegmentRole.VOLATILE, f"Mode: {review_packet['mode']}. Give your verdict.")),
        VALIDATOR_SCHEMA, max_output_tokens=1024)


def packet_from_request(request, title):
    """The JSON packet in a request's HANDOFF segment (used by simulated agents and tests)."""
    for segment in request.segments:
        if segment.role is SegmentRole.HANDOFF and segment.content.startswith(title):
            return json.loads(segment.content.split("\n", 1)[1])
    raise ValueError(f"request has no {title}")


def verdict_problems(verdict, card):
    """Deterministic checks on a validator reply: every criterion addressed once; accept only when
    every finding is satisfied. Problems turn the verdict into escalate."""
    problems = []
    ids = [c.get("id") for c in verdict["criteria"]]
    if sorted(ids) != sorted(card.criterion_ids):
        problems.append(f"validator addressed criteria {sorted(ids)}; the card has {sorted(card.criterion_ids)}")
    if verdict["verdict"] == "accept" and not all(c["satisfied"] for c in verdict["criteria"]):
        problems.append("validator accepted with an unsatisfied criterion")
    return problems


def sampled(run_id, rate):
    """Deterministic sample: the same run is always in or out for a given rate."""
    if rate <= 0:
        return False
    return int(sha256(run_id.encode()).hexdigest()[:8], 16) / 0x100000000 < rate


def packet_digest(packet):
    return sha256(canonical(packet).encode()).hexdigest()
