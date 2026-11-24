"""Deterministic stand-ins for the planner and plan reviewer (CI and the Phase 9 eval).

SimulatedPlanner follows a script, one step per attempt (the number of "Previous attempts" it is
shown): a step is a plan {"calls": [...]}, {"escalate": reason}, or {"raw": text}. With no step left
it repeats the last one.

SimulatedPlanReviewer gives one finding per card criterion; reject=True rejects every plan.
"""
import json

from ..context.tags import parse_line
from ..models.socket import SOCKET_VERSION, Capabilities, ModelReply, Usage


def _usage(request, body):
    return Usage(len(request.text()) // 4, len(body) // 4, exact=False)


def _section(request, title):
    for segment in request.segments:
        if segment.content.startswith(title):
            return json.loads(segment.content.split("\n", 1)[1])
    return None


class SimulatedPlanner:
    socket_version = SOCKET_VERSION

    def __init__(self, script, name="simulated-planner-v1"):
        self.name, self.script, self.calls = name, list(script), 0
        self.capabilities = Capabilities(context_window=200_000)
        self.seen = []

    def complete(self, request):
        self.calls += 1
        self.seen.append(request.text())
        attempt = len(_section(request, "Previous attempts") or [])
        step = self.script[min(attempt, len(self.script) - 1)]
        body = step["raw"] if "raw" in step else json.dumps(step, sort_keys=True)
        return ModelReply(body, self.name, _usage(request, body))


class SimulatedPlanReviewer:
    socket_version = SOCKET_VERSION

    def __init__(self, name="simulated-plan-reviewer-v1", *, reject=False):
        self.name, self.reject, self.calls = name, reject, 0
        self.capabilities = Capabilities(context_window=200_000)
        self.seen = []

    def complete(self, request):
        self.calls += 1
        self.seen.append(request.text())
        criteria = [t.positional[0] for t in map(parse_line, request.text().splitlines())
                    if t is not None and t.kind == "criterion" and t.positional]
        ok = not self.reject
        findings = [{"id": c, "satisfied": ok, "finding": "served by the plan" if ok else "the plan answers another question"}
                    for c in criteria]
        body = json.dumps({"verdict": "accept" if ok else "reject",
                           "reasons": ["every criterion is served" if ok else "the plan does not answer the card"],
                           "criteria": findings}, sort_keys=True)
        return ModelReply(body, self.name, _usage(request, body))
