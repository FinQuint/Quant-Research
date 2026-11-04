"""How the context layer writes items into a request, and how they are read back.

Every item in a request is one tagged line, so evidence always carries its ID and version
(spec: "Retrieved evidence, each item tagged with its evidence ID and version"):

    [card run=R task=T code_sha=S datasets=prices@v3,rates@v1] goal text
    [criterion C1 topic=duration] criterion text
    [evidence ev-7 v2 run=R topic=duration dataset=prices@v3] content
    [mention ev-7 v1 topic=duration] free text that refers to evidence (notes, summaries)
    [handoff from=researcher to=analyst] {"json": "payload"}
    [metric run:R/analytics field=bond_price] 104.4912925   a value a report may cite by placeholder

REPORT_SCHEMA is the output contract for a task report: every criterion addressed, each with
the evidence it relies on.
"""
from dataclasses import dataclass
import re

_TOKEN = r"[A-Za-z0-9_.@,:/-]+"
_LINE = re.compile(r"^\[(card|criterion|evidence|mention|handoff|metric)((?:\s+" + _TOKEN + r"(?:=" + _TOKEN + r")?)*)\]\s?(.*)$")

REPORT_SCHEMA = {
    "type": "object",
    "required": ["run_id", "criteria"],
    "additionalProperties": False,
    "properties": {
        "run_id": {"type": "string"},
        "code_sha": {"type": "string"},
        "criteria": {"type": "array", "items": {
            "type": "object", "required": ["id", "citations"], "additionalProperties": False,
            "properties": {
                "id": {"type": "string", "minLength": 1},
                "citations": {"type": "array", "items": {
                    "type": "object", "required": ["evidence_id", "version"], "additionalProperties": False,
                    "properties": {"evidence_id": {"type": "string", "minLength": 1},
                                   "version": {"type": "string", "minLength": 1}}}},
            }}},
        "claims": {"type": "object"},
    },
}


def _clean(value):
    text = str(value)
    if not text or not re.fullmatch(_TOKEN, text):
        raise ValueError(f"tag value {text!r} may use only letters, digits and _ . @ , : / -")
    return text


def _one_line(text):
    return " ".join(str(text).split())


def card_line(run_id, task_id, code_sha, datasets, goal):
    pairs = ",".join(f"{_clean(k)}@{_clean(v)}" for k, v in sorted(datasets.items()))
    return (f"[card run={_clean(run_id)} task={_clean(task_id)} code_sha={_clean(code_sha)}"
            f" datasets={pairs or '-'}] {_one_line(goal)}")


def criterion_line(criterion_id, topic, text):
    return f"[criterion {_clean(criterion_id)} topic={_clean(topic)}] {_one_line(text)}"


def evidence_line(evidence_id, version, run_id, topic, content, dataset=None):
    extra = f" dataset={_clean(dataset)}" if dataset else ""
    return (f"[evidence {_clean(evidence_id)} v{_clean(version)} run={_clean(run_id or '-')}"
            f" topic={_clean(topic)}{extra}] {_one_line(content)}")


def mention_line(evidence_id, version, topic, text):
    return f"[mention {_clean(evidence_id)} v{_clean(version)} topic={_clean(topic)}] {_one_line(text)}"


def metric_line(artifact_id, field, value):
    return f"[metric {_clean(artifact_id)} field={_clean(field)}] {_one_line(value)}"


def handoff_line(source, target, payload_json):
    return f"[handoff from={_clean(source)} to={_clean(target)}] {_one_line(payload_json)}"


@dataclass(frozen=True)
class Tag:
    kind: str
    positional: tuple
    fields: dict
    text: str


def parse_line(line):
    """A Tag for a tagged line, or None."""
    match = _LINE.match(line.strip())
    if not match:
        return None
    positional, fields = [], {}
    for part in match.group(2).split():
        if "=" in part:
            key, value = part.split("=", 1)
            fields[key] = value
        else:
            positional.append(part)
    return Tag(match.group(1), tuple(positional), fields, match.group(3))


def parse_ref(tag):
    """(evidence_id, version) of an evidence or mention tag."""
    evidence_id, version = tag.positional[0], tag.positional[1]
    return evidence_id, version[1:] if version.startswith("v") else version
