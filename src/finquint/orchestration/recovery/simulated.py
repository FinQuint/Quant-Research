"""Deterministic stand-ins for the recovery agents, used by CI and the Phase 8 eval.

SimulatedRepairer follows a script, one step per attempt. A step is a dict:
  {"set": {path: value}}       change these parameters (on top of the original call)
  {"method": name}             swap the method (may be combined with "set")
  {"lie": True}                misreport reflection.changes (the reflection guard must catch it)
  {"escalate": reason}         ask for escalation instead of proposing
  {"raw": text}                reply with this text verbatim (schema faults)
Its reflection is honest by default: evidence is quoted from the latest failure text in the
packet and changes are the real diff. With no step left it repeats its last proposal (which the
novelty guard must catch).

SimulatedValidator reads only its review packet and the task card. Documented simulation
assumption: a criterion whose topic names a parameter means "this parameter defines the
question", so a repair that changed that parameter is goal drift and is rejected. Any failed gate
is rejected too. reject_samples=True rejects every policy-path sample.
"""
import json
import re

from ..context.tags import parse_line
from ..models.socket import SOCKET_VERSION, Capabilities, ModelReply, Usage
from .agents import packet_from_request
from .guards import param_diff
from .ladder import set_path


_DETAILS = re.compile(r"'details': '([^']{8,})'")


def _usage(request, body):
    return Usage(len(request.text()) // 4, len(body) // 4, exact=False)


class SimulatedRepairer:
    socket_version = SOCKET_VERSION

    def __init__(self, script, name="simulated-repairer-v1"):
        self.name, self.script = name, list(script)
        self.capabilities = Capabilities(context_window=200_000)
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        packet = packet_from_request(request, "Failure packet")
        attempt = len(packet["attempts"])
        step = self.script[min(attempt, len(self.script) - 1)] if self.script else {}
        if "raw" in step:
            return ModelReply(step["raw"], self.name, _usage(request, step["raw"]))
        original = packet["original_call"]
        method = step.get("method", original["method"])
        params = original["parameters"]
        for path, value in sorted(step.get("set", {}).items()):
            params = set_path(params, path, value)
        latest = packet["attempts"][-1]["failure"] if packet["attempts"] else packet["failure"]
        changes = list(param_diff(original["parameters"], params)) + (["method"] if method != original["method"] else [])
        if step.get("lie"):
            changes = sorted(set(changes) ^ {"annualization"})
        details = _DETAILS.search(latest)
        evidence = details.group(1) if details else (latest[:60].strip() or latest)
        reflection = {"diagnosis": f"The check failed: {evidence}",
                      "evidence": evidence,
                      "hypothesis": "The listed change addresses the failed check.",
                      "changes": sorted(changes),
                      "differs_from_previous": f"attempt {attempt + 1} uses a different call"}
        reply = {"reflection": reflection}
        if step.get("escalate"):
            reply["escalate"] = step["escalate"]
        else:
            reply["proposal"] = {"method": method, "parameters": params}
        body = json.dumps(reply, sort_keys=True)
        return ModelReply(body, self.name, _usage(request, body))


class SimulatedValidator:
    socket_version = SOCKET_VERSION

    def __init__(self, name="simulated-validator-v1", *, reject_samples=False, skip_criteria=False):
        self.name, self.reject_samples, self.skip_criteria = name, reject_samples, skip_criteria
        self.capabilities = Capabilities(context_window=200_000)
        self.calls = 0
        self.seen = []   # the review packets it was shown (tests check it is blind)

    def complete(self, request):
        self.calls += 1
        packet = packet_from_request(request, "Review packet")
        self.seen.append(request.text())
        criteria = []
        for line in request.text().splitlines():
            tag = parse_line(line)
            if tag is not None and tag.kind == "criterion" and tag.positional:
                criteria.append((tag.positional[0], tag.fields.get("topic")))
        gates = packet.get("gates")
        gate_list = gates if isinstance(gates, list) else [g for v in (gates or {}).values() for g in v]
        failed_gate = any(not g.get("passed") for g in gate_list)
        changed = set(packet.get("changed", ()))
        findings, reasons = [], []
        for cid, topic in criteria:
            drift = topic in changed
            ok = not drift and not failed_gate and not (packet["mode"] == "sample" and self.reject_samples)
            findings.append({"id": cid, "satisfied": ok,
                             "finding": f"{topic} was changed by the repair" if drift else "consistent with the card"})
            if drift:
                reasons.append(f"goal drift: the repair changed {topic}, which the task fixes")
        if self.skip_criteria and findings:
            findings = findings[1:]
        verdict = "accept" if findings and all(f["satisfied"] for f in findings) else "reject"
        if not reasons:
            reasons = ["every criterion is satisfied"] if verdict == "accept" else ["a criterion is not satisfied"]
        body = json.dumps({"verdict": verdict, "reasons": reasons, "criteria": findings}, sort_keys=True)
        return ModelReply(body, self.name, _usage(request, body))
