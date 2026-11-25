"""Deterministic stand-ins for the coding agent and the code reviewer (CI and the Phase 10 eval).

SimulatedCoder follows a script, one step per attempt (the number of "Previous attempts" it is
shown): a step is {"module": ..., "source": ...}, {"escalate": reason} or {"raw": text}. With no
step left it repeats the last one.

SimulatedCodeReviewer gives one finding per card criterion; reject=True rejects every module.
"""
import json

from ..models.socket import SOCKET_VERSION, Capabilities, ModelReply, Usage
from ..planning.simulated import SimulatedPlanReviewer, _section, _usage


class SimulatedCoder:
    socket_version = SOCKET_VERSION

    def __init__(self, script, name="simulated-coder-v1"):
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


class SimulatedCodeReviewer(SimulatedPlanReviewer):
    def __init__(self, name="simulated-code-reviewer-v1", *, reject=False):
        super().__init__(name, reject=reject)


__all__ = ["SimulatedCodeReviewer", "SimulatedCoder"]
