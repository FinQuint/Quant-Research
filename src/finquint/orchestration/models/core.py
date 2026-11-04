"""The core side of the socket: the same safety behavior for every model.

ModelCaller.call(request):
  1. Tries the primary plug, then each fallback, in order. A plug whose window is too small for
     the request is given a rebuilt request (fit=...) or skipped.
  2. ModelUnavailable (or any unexpected plug exception) moves on to the next plug. When every
     plug is unavailable it raises a plain RetryableError. The autonomous runtime records errors
     as "<class>: message" and the gate adapter (status_for_task) recognizes the "RetryableError"
     prefix, so the gate sees an INFRA_ERROR and the retry budget applies.
  3. The reply must be JSON that matches request.output_schema. One repair retry is made with
     the validation errors appended; still invalid -> ModelEscalation (spec: "Model call layer").
  4. ModelRefused -> ModelEscalation; a refusal is never retried on another model.
  5. The result records which model produced it.
"""
from dataclasses import dataclass
import json

from finquint.autonomous import RetryableError

from ..tracing import NULL_TRACER
from .schema import validate
from .socket import ModelRefused, ModelUnavailable, Segment, SegmentRole, check_plug
from .tokens import TokenCounter

MAX_REPAIRS = 1   # spec: one repair retry with the validation error, then ESCALATE


class ModelEscalation(Exception):
    """The model layer cannot produce a valid output; route the branch to ESCALATE."""

    def __init__(self, reason, *, model=None, errors=()):
        super().__init__(reason)
        self.reason, self.model, self.errors = reason, model, tuple(errors)


@dataclass(frozen=True)
class ModelResult:
    output: object
    model: str
    attempts: int          # replies received, including the repair
    replies: tuple
    repaired: bool
    skipped: tuple = ()    # (plug name, reason) for plugs that were not used


def parse_json(text):
    """JSON from a reply; a single surrounding ``` fence is allowed."""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
    return json.loads(body)


def check_output(text, schema):
    """(value, errors). errors is empty when the text is JSON matching the schema."""
    try:
        value = parse_json(text)
    except (json.JSONDecodeError, ValueError) as exc:
        return None, [f"reply is not valid JSON: {exc}"]
    return value, validate(value, schema)


def repair_segment(errors):
    lines = "\n".join(f"- {error}" for error in errors)
    return Segment(SegmentRole.VOLATILE,
                   "Your previous reply did not match the required output schema:\n"
                   f"{lines}\nReply again with only JSON that matches the schema.")


class ModelCaller:
    """fit(request, plug) -> request: rebuild a request for a plug with a smaller window.
    Without fit, such a plug is skipped."""

    def __init__(self, plug, *, fallbacks=(), tracer=None, calibration=None, fit=None, max_repairs=MAX_REPAIRS):
        self.plugs = tuple(check_plug(p) for p in (plug, *fallbacks))
        self.tracer = tracer or NULL_TRACER
        self.calibration = calibration
        self.fit = fit
        if type(max_repairs) is not int or max_repairs < 0:
            raise ValueError("max_repairs must be a non-negative integer")
        self.max_repairs = max_repairs

    def counter(self, plug):
        return TokenCounter(plug, calibration=self.calibration)

    def _complete(self, plug, request):
        counter = self.counter(plug)
        with self.tracer.span("model.call", run_id=request.run_id, task_id=request.task_id,
                              model=plug.name) as span:
            reply = plug.complete(request)
            span.set(model=reply.model, input_tokens=reply.usage.input_tokens,
                     output_tokens=reply.usage.output_tokens)
        if self.calibration is not None and reply.usage.exact and not counter.exact:
            self.calibration.observe(plug.name, counter.raw_estimate(request.text()), reply.usage.input_tokens)
        return reply

    def _fits(self, plug, request):
        need = self.counter(plug).count(request.text()) + request.max_output_tokens
        return need <= plug.capabilities.context_window

    def call(self, request):
        skipped, outages = [], []
        for plug in self.plugs:
            current = request
            if not self._fits(plug, current):
                if self.fit is None:
                    skipped.append((plug.name, "context window too small"))
                    continue
                try:
                    current = self.fit(current, plug)
                except Exception as exc:   # e.g. the card alone does not fit this window
                    skipped.append((plug.name, f"rebuild failed: {type(exc).__name__}: {exc}"))
                    continue
                if not self._fits(plug, current):
                    skipped.append((plug.name, "context window too small after rebuild"))
                    continue
            replies = []
            try:
                reply = self._complete(plug, current)
                replies.append(reply)
                value, errors = check_output(reply.text, current.output_schema)
                repairs = 0
                while errors and repairs < self.max_repairs:
                    repairs += 1
                    repaired = current.with_segment(repair_segment(errors))
                    if not self._fits(plug, repaired):
                        errors = errors + ["the repair request does not fit the model's window"]
                        break
                    current = repaired
                    reply = self._complete(plug, current)
                    replies.append(reply)
                    value, errors = check_output(reply.text, current.output_schema)
            except ModelRefused as exc:
                raise ModelEscalation(f"model {plug.name} refused the request: {exc}", model=plug.name) from exc
            except ModelUnavailable as exc:
                outages.append(f"{plug.name}: {exc}")
                continue
            except Exception as exc:  # a broken plug counts as unavailable, never as an answer
                outages.append(f"{plug.name}: {type(exc).__name__}: {exc}")
                continue
            if errors:
                raise ModelEscalation(f"model {reply.model} output failed the schema after "
                                      f"{self.max_repairs} repair(s)", model=reply.model, errors=errors)
            return ModelResult(value, reply.model, len(replies), tuple(replies), len(replies) > 1, tuple(skipped))
        detail = "; ".join(outages + [f"{name}: {why}" for name, why in skipped]) or "no plug available"
        if not outages:
            raise ModelEscalation(f"no plug can take this request ({detail})")
        raise RetryableError(f"every model is unavailable ({detail})")
