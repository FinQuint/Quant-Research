"""Claude plug: the model socket's adapter for Anthropic's Messages API (Phase 7, step 2).

Optional install. Nothing else in finquint imports this module, and this module imports the
`anthropic` SDK only when no client is passed in, so CI and the core never need it:

    pip install anthropic            # then set ANTHROPIC_API_KEY
    from finquint.orchestration.models.claude import ClaudePlug
    caller = ModelCaller(ClaudePlug())

The plug only translates. It never validates, retries on another model, or escalates; the core
(core.py) does that for every plug. How a request is mapped:

  * SYSTEM segments become the system prompt; every other segment becomes a text block of one
    user message, in order. The last segment marked stable gets a cache breakpoint, so the
    system prompt, task card and handoffs are cached across calls of a task.
  * The output schema is stated as an instruction at the end of the system prompt. With
    structured_output=True it is also sent as output_config.format (off by default: the core's
    schema subset may use keywords the API's structured outputs do not accept).
  * Thinking is left to the model's default (adaptive on current models); depth is set with
    `effort` (tune). max_tokens = the request's output reserve + thinking_headroom (tune),
    because thinking tokens count against max_tokens. Requests are streamed so a large output
    reserve cannot hit the SDK's non-streaming timeout guard.

Errors are mapped by status code, without importing the SDK:
  429, 408, 409, 5xx, 529, connection errors and timeouts -> ModelUnavailable (core falls back,
  then INFRA_ERROR); any other API error (400, 401, 403, 404, 413, ...) and a "refusal" stop
  reason -> ModelRefused (core escalates; never retried on another model).

Server-side refusal fallbacks are off by default because the core's rule is that a refusal is
never answered by a different model. server_fallbacks=... turns them on explicitly.
"""
from functools import lru_cache
import json

from .socket import SOCKET_VERSION, Capabilities, ModelRefused, ModelReply, ModelUnavailable, SegmentRole, Usage

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_CONTEXT_WINDOW = 200_000   # (tune) the model accepts 1M; budgets are a share of this, so it caps cost
DEFAULT_EFFORT = "high"            # (tune) low | medium | high | xhigh | max
DEFAULT_THINKING_HEADROOM = 8_000  # (tune) extra max_tokens for adaptive thinking
EFFORTS = ("low", "medium", "high", "xhigh", "max")

_UNAVAILABLE_STATUS = {408, 409, 429}
_UNAVAILABLE_NAMES = {"APIConnectionError", "APITimeoutError"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


def schema_instruction(schema):
    return ("Reply with only a JSON value that matches this JSON schema. No prose, no code fence.\n"
            + json.dumps(schema, sort_keys=True, separators=(",", ":")))


def map_error(exc):
    """The socket error for an SDK exception, or None when it is not an API error."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status in _UNAVAILABLE_STATUS or status >= 500:
            return ModelUnavailable(f"Claude API {status}: {exc}")
        return ModelRefused(f"Claude API {status}: {exc}")
    names = {cls.__name__ for cls in type(exc).__mro__}
    if names & _UNAVAILABLE_NAMES or isinstance(exc, (ConnectionError, TimeoutError)):
        return ModelUnavailable(f"Claude API unreachable: {type(exc).__name__}: {exc}")
    return None


class ClaudePlug:
    """ModelPlug for Claude. client: an `anthropic.Anthropic()` or anything with the same
    `messages.stream` / `messages.count_tokens` surface (tests pass a fake)."""

    socket_version = SOCKET_VERSION

    def __init__(self, model=DEFAULT_MODEL, *, client=None, context_window=DEFAULT_CONTEXT_WINDOW,
                 effort=DEFAULT_EFFORT, thinking_headroom=DEFAULT_THINKING_HEADROOM, exact_tokens=False,
                 structured_output=False, server_fallbacks=None, cache=True, max_retries=2, timeout=600.0):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a model ID")
        if effort is not None and effort not in EFFORTS:
            raise ValueError(f"effort must be one of {EFFORTS} or None")
        if type(thinking_headroom) is not int or thinking_headroom < 0:
            raise ValueError("thinking_headroom must be a non-negative integer")
        if client is None:
            try:
                import anthropic
            except ImportError as exc:  # optional install
                raise ImportError("ClaudePlug needs the Anthropic SDK: pip install anthropic") from exc
            client = anthropic.Anthropic(max_retries=max_retries, timeout=timeout)
        self.client = client
        self.model = model
        self.name = f"claude:{model}"
        self.effort = effort
        self.thinking_headroom = thinking_headroom
        self.structured_output = structured_output
        self.server_fallbacks = server_fallbacks
        self.cache = cache
        self.capabilities = Capabilities(context_window=context_window, prompt_caching=cache,
                                         native_structured_output=structured_output,
                                         exact_token_count=exact_tokens)
        self._count = lru_cache(maxsize=4096)(self._count_uncached)

    # --- optional exact counter -----------------------------------------------------------

    def count_tokens(self, text):
        """Exact count from the API (one network call per distinct text, cached). Counts the
        text as one user message, so it includes a few tokens of message overhead: overcounts."""
        return self._count(text)

    def _count_uncached(self, text):
        try:
            result = self.client.messages.count_tokens(
                model=self.model, messages=[{"role": "user", "content": text or " "}])
        except Exception as exc:
            mapped = map_error(exc)
            raise mapped from exc if mapped else exc
        return int(result.input_tokens)

    # --- request mapping ------------------------------------------------------------------

    def build_params(self, request):
        """The Messages API arguments for a ModelRequest (pure; no network)."""
        system, content = [], []
        for segment in request.segments:
            if not segment.content:
                continue
            block = {"type": "text", "text": segment.content}
            (system if segment.role is SegmentRole.SYSTEM else content).append((segment, block))
        if self.cache:
            stable = [block for segment, block in system + content if segment.stable]
            if stable:
                stable[-1]["cache_control"] = {"type": "ephemeral"}
        system_blocks = [block for _, block in system]
        # the schema is fixed per task, so it may sit inside the cached prefix
        system_blocks.append({"type": "text", "text": schema_instruction(request.output_schema)})
        user_blocks = [block for _, block in content] or [{"type": "text", "text": "Reply now."}]
        params = {"model": self.model,
                  "max_tokens": request.max_output_tokens + self.thinking_headroom,
                  "system": system_blocks,
                  "messages": [{"role": "user", "content": user_blocks}]}
        output_config = {}
        if self.effort is not None:
            output_config["effort"] = self.effort
        if self.structured_output:
            output_config["format"] = {"type": "json_schema", "schema": request.output_schema}
        if output_config:
            params["output_config"] = output_config
        if self.server_fallbacks is not None:
            params["extra_headers"] = {"anthropic-beta": _FALLBACK_BETA}
            params["extra_body"] = {"fallbacks": self.server_fallbacks}
        return params

    # --- the socket call ------------------------------------------------------------------

    def complete(self, request):
        params = self.build_params(request)
        try:
            with self.client.messages.stream(**params) as stream:
                message = stream.get_final_message()
        except Exception as exc:
            mapped = map_error(exc)
            if mapped is None:
                raise
            raise mapped from exc
        if getattr(message, "stop_reason", None) == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) if details is not None else None
            raise ModelRefused(f"{getattr(message, 'model', self.model)} declined the request"
                               + (f" ({category})" if category else ""))
        text = "".join(block.text for block in message.content if getattr(block, "type", None) == "text")
        usage = message.usage
        cached = (getattr(usage, "cache_read_input_tokens", None) or 0) + \
                 (getattr(usage, "cache_creation_input_tokens", None) or 0)
        return ModelReply(text, getattr(message, "model", None) or self.model,
                          Usage(int(usage.input_tokens) + int(cached), int(usage.output_tokens), exact=True))
