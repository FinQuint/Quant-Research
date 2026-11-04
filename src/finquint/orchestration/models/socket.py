"""The model socket: the one interface between the workflow and any language model.

The context builder produces a provider-neutral ModelRequest. A plug converts it to its
provider's format and returns a ModelReply. Plugs never validate, retry, escalate or pick a
fallback; the core (core.py) does that, the same way for every model.

Hints, not commands: a Segment marked stable may be cached by a plug that supports caching
and is ignored by one that does not. The request's output_schema is always enforced by the
core; a plug with native structured output just passes it to the provider as well.
"""
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
import json
from typing import Protocol, runtime_checkable

from finquint.autonomous import RetryableError

from .schema import check_schema

SOCKET_VERSION = 1


class SegmentRole(str, Enum):
    SYSTEM = "system"          # system prompt and tool schemas (stable per agent type)
    TASK_CARD = "task_card"    # pinned task card, verbatim on every call
    HANDOFF = "handoff"        # typed records from upstream agents
    EVIDENCE = "evidence"      # retrieved evidence, tagged with ID and version
    HISTORY = "history"        # recent working history
    VOLATILE = "volatile"      # budget left, the current instruction, repair notes


@dataclass(frozen=True)
class Segment:
    role: SegmentRole
    content: str
    stable: bool = False       # hint: identical across calls of this task, safe to cache
    ids: tuple = ()            # evidence or criterion IDs this segment carries

    def __post_init__(self):
        object.__setattr__(self, "role", SegmentRole(self.role))
        if not isinstance(self.content, str):
            raise TypeError("segment content must be text")
        object.__setattr__(self, "ids", tuple(self.ids))


@dataclass(frozen=True)
class ModelRequest:
    """Everything a plug needs. Segments come in cache order: stable first, volatile last."""
    run_id: str
    task_id: str
    segments: tuple
    output_schema: dict
    max_output_tokens: int = 1024

    def __post_init__(self):
        object.__setattr__(self, "segments", tuple(self.segments))
        if not self.segments or not all(isinstance(s, Segment) for s in self.segments):
            raise ValueError("a request needs at least one Segment")
        if type(self.max_output_tokens) is not int or self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be a positive integer")
        check_schema(self.output_schema)

    def text(self):
        """The whole request as plain text, in order (for plugs without native roles)."""
        return "\n\n".join(s.content for s in self.segments if s.content)

    def digest(self):
        """Stable fingerprint of the request; replay recordings are keyed by it."""
        body = {"run_id": self.run_id, "task_id": self.task_id, "max_output_tokens": self.max_output_tokens,
                "output_schema": self.output_schema,
                "segments": [[s.role.value, s.content, s.stable, list(s.ids)] for s in self.segments]}
        return sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def with_segment(self, segment):
        return ModelRequest(self.run_id, self.task_id, self.segments + (segment,), self.output_schema,
                            self.max_output_tokens)


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    exact: bool = False        # True when the provider reported the counts


@dataclass(frozen=True)
class ModelReply:
    text: str
    model: str                 # the exact model that produced this reply
    usage: Usage = field(default_factory=lambda: Usage(0, 0))


@dataclass(frozen=True)
class Capabilities:
    context_window: int                   # input + output tokens the model accepts
    prompt_caching: bool = False
    native_structured_output: bool = False
    tool_calls: bool = False
    exact_token_count: bool = False       # the plug has count_tokens()

    def __post_init__(self):
        if type(self.context_window) is not int or self.context_window < 1:
            raise ValueError("context_window must be a positive integer")


@runtime_checkable
class ModelPlug(Protocol):
    name: str
    socket_version: int
    capabilities: Capabilities

    def complete(self, request: ModelRequest) -> ModelReply: ...


@runtime_checkable
class CountsTokens(Protocol):
    """Optional: a plug with an exact tokenizer."""

    def count_tokens(self, text: str) -> int: ...


# --- errors a plug may raise (anything else is treated as ModelUnavailable) ----------------

class ModelUnavailable(RetryableError):
    """Timeout, rate limit or outage. The core tries the next fallback; when none is left it
    raises a plain RetryableError, which the gate sees as an INFRA_ERROR."""


class ModelRefused(Exception):
    """The provider declined the request (content policy, request too large). Not retried."""


class IncompatiblePlug(ValueError):
    """The plug was written for a different socket version or declares invalid capabilities."""


def check_plug(plug):
    """Fail closed on a plug the core cannot rely on."""
    if not isinstance(plug, ModelPlug):
        raise IncompatiblePlug(f"{plug!r} does not implement the ModelPlug interface")
    if getattr(plug, "socket_version", None) != SOCKET_VERSION:
        raise IncompatiblePlug(f"plug {plug.name!r} targets socket version {plug.socket_version}, "
                               f"this core speaks {SOCKET_VERSION}")
    if not isinstance(plug.capabilities, Capabilities):
        raise IncompatiblePlug(f"plug {plug.name!r} must declare Capabilities")
    if plug.capabilities.exact_token_count and not isinstance(plug, CountsTokens):
        raise IncompatiblePlug(f"plug {plug.name!r} claims exact_token_count but has no count_tokens()")
    if not isinstance(plug.name, str) or not plug.name.strip():
        raise IncompatiblePlug("a plug needs a name")
    return plug
