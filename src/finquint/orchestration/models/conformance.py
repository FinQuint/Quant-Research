"""Conformance checks every plug must pass before the core relies on it.

    failures = check_conformance(plug, request)     # [] means the plug conforms

request: a ModelRequest the plug can answer (its output_schema is what the plug is expected
to meet). outage: optional zero-argument callable that returns the same plug in an outage
state; the plug must then raise ModelUnavailable, never return a reply or a different error.
"""
from .core import ModelCaller, ModelEscalation, check_output
from .socket import CountsTokens, IncompatiblePlug, ModelReply, ModelUnavailable, Usage, check_plug


def check_conformance(plug, request, *, outage=None):
    failures = []
    try:
        check_plug(plug)
    except IncompatiblePlug as exc:
        return [f"interface: {exc}"]

    try:
        reply = plug.complete(request)
    except Exception as exc:
        return failures + [f"complete: raised {type(exc).__name__}: {exc}"]
    if not isinstance(reply, ModelReply):
        return failures + ["complete: must return a ModelReply"]
    if not isinstance(reply.text, str):
        failures.append("reply: text must be a string")
    if not isinstance(reply.model, str) or not reply.model.strip():
        failures.append("reply: must name the model that produced it")
    usage = reply.usage
    if not isinstance(usage, Usage) or any(type(n) is not int or n < 0
                                           for n in (usage.input_tokens, usage.output_tokens)):
        failures.append("reply: usage must hold non-negative integer token counts")
    _, errors = check_output(reply.text, request.output_schema)
    if errors:
        failures.append(f"reply: does not match the request's output schema ({errors[0]})")

    if plug.capabilities.exact_token_count and isinstance(plug, CountsTokens):
        short, long = plug.count_tokens("a"), plug.count_tokens("a" * 400)
        if type(short) is not int or short < 0 or long < short:
            failures.append("count_tokens: must return non-negative integers that grow with the text")

    try:
        result = ModelCaller(plug).call(request)
        if result.model != reply.model:
            failures.append("core: the result must record the model that answered")
    except ModelEscalation as exc:
        failures.append(f"core: valid request escalated ({exc})")
    except Exception as exc:
        failures.append(f"core: raised {type(exc).__name__}: {exc}")

    if outage is not None:
        try:
            outage().complete(request)
            failures.append("outage: returned a reply instead of raising ModelUnavailable")
        except ModelUnavailable:
            pass
        except Exception as exc:
            failures.append(f"outage: raised {type(exc).__name__}, expected ModelUnavailable")
    return failures
