"""Host-owned tool allowlist; every tool declares its side-effect class."""
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from finquint.autonomous.models import identifier


class SideEffect(str, Enum):
    READ_ONLY = "read_only"                # no token, no ledger
    IDEMPOTENT_WRITE = "idempotent_write"  # token + ledger; safe to repeat after a crash
    EXTERNAL = "external"                  # token + ledger + outbox (merges, deploys, data writes)


@dataclass(frozen=True)
class Tool:
    """handler(action, idempotency_key) -> JSON dict.

    reconcile(action, idempotency_key) -> JSON dict if the effect already happened, else None.
    Only EXTERNAL tools take one; without it, an interrupted dispatch is left in doubt
    for a human instead of being re-sent.
    """
    name: str
    side_effect: SideEffect
    handler: Callable
    reconcile: Callable | None = None

    def __post_init__(self):
        identifier(self.name)
        if not isinstance(self.side_effect, SideEffect):
            raise ValueError("tool must declare a SideEffect class")
        if not callable(self.handler):
            raise ValueError("tool handler must be callable")
        if self.reconcile is not None and (self.side_effect is not SideEffect.EXTERNAL or not callable(self.reconcile)):
            raise ValueError("only external tools take a callable reconcile check")

    @property
    def requires_token(self):
        return self.side_effect is not SideEffect.READ_ONLY

    @property
    def uses_outbox(self):
        return self.side_effect is SideEffect.EXTERNAL


class ToolRegistry:
    def __init__(self):
        self._tools = {}

    def register(self, tool):
        if not isinstance(tool, Tool):
            raise TypeError("expected Tool")
        if tool.name in self._tools:
            raise ValueError("tool already registered")
        self._tools[tool.name] = tool
        return self

    def get(self, name):
        if name not in self._tools:
            raise ValueError(f"tool is not registered: {name}")
        return self._tools[name]

    def names(self):
        return frozenset(self._tools)
