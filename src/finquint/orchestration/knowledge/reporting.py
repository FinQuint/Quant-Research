"""Writing a report whose every number traces to an artifact (ADR-007).

write_report() asks a model (through the socket core) for a report in REPORT_TEXT_SCHEMA, renders
its {{metric:...}} placeholders from a MetricStore, and runs the numeral post-check. A report with
an unresolved placeholder or an untraced number is sent back once with the problems listed; if it
still fails, the result is ESCALATE and no report is produced, so an untraced number never
reaches a final output.
"""
from dataclasses import dataclass

from ..context.tags import metric_line, parse_line
from ..models.core import ModelEscalation
from ..models.socket import Segment, SegmentRole
from ..verify.numbers import RenderError, check_numerals, render, resolve

REPORT_TEXT_SCHEMA = {
    "type": "object", "required": ["run_id", "sections"], "additionalProperties": False,
    "properties": {
        "run_id": {"type": "string"},
        "sections": {"type": "array", "minItems": 1, "items": {
            "type": "object", "required": ["id", "text"], "additionalProperties": False,
            "properties": {"id": {"type": "string", "minLength": 1}, "text": {"type": "string", "minLength": 1}}}},
    },
}


def metric_segment(metrics, refs):
    """An evidence segment listing each (artifact_id, field) with its value, so the model knows
    what it may cite. Raises KeyError for a ref the store cannot resolve."""
    lines = [metric_line(artifact_id, field, resolve(metrics, artifact_id, field)) for artifact_id, field in refs]
    return Segment(SegmentRole.EVIDENCE, "\n".join(lines), ids=tuple(a for a, _ in refs))


def request_identifiers(request):
    """Tokens in the request's tag lines that may legitimately contain digits."""
    found = set()
    for line in request.text().splitlines():
        tag = parse_line(line)
        if tag is None:
            continue
        found.update(tag.positional)          # versions appear here as "v2", never as "2"
        for key in ("run", "task", "code_sha", "field"):
            if key in tag.fields:
                found.add(tag.fields[key])
    # An all-digit token would exempt that number everywhere in the report.
    return {token for token in found if any(c.isalpha() for c in token)}


@dataclass(frozen=True)
class ReportResult:
    status: str                 # "ok" or "escalated"
    text: str = ""
    numbers: tuple = ()         # RenderedNumber items in the final text
    untraced: tuple = ()        # Untraced items in the last attempt
    problems: tuple = ()
    model_calls: int = 0
    model: str | None = None


def _repair(problems):
    listing = "\n".join(f"- {p}" for p in problems)
    return Segment(SegmentRole.VOLATILE,
                   "Your report contains numbers that do not trace to an artifact:\n" + listing +
                   "\nWrite every number as {{metric:ARTIFACT_ID:FIELD}} using the [metric ...] lines.")


def write_report(caller, request, *, metrics, inputs=(), max_repairs=1):
    identifiers = request_identifiers(request)
    calls, problems, untraced, model = 0, (), (), None
    for _ in range(1 + max_repairs):
        try:
            result = caller.call(request)
        except ModelEscalation as exc:
            return ReportResult("escalated", problems=(str(exc),), model_calls=calls + 1, model=exc.model)
        calls += result.attempts
        model = result.model
        text = "\n".join(f"{s['id']}: {s['text']}" for s in result.output["sections"])
        try:
            rendered = render(text, metrics)
        except RenderError as exc:
            problems, untraced = exc.errors, ()
        else:
            untraced = tuple(check_numerals(rendered, identifiers=identifiers, inputs=inputs))
            problems = tuple(f"untraced number {u.text!r}" for u in untraced)
            if not problems:
                return ReportResult("ok", rendered.text, rendered.numbers, (), (), calls, model)
        request = request.with_segment(_repair(problems))
    return ReportResult("escalated", "", (), untraced, problems, calls, model)
