"""Number provenance (ADR-007): placeholder rendering and the numeral post-check.

Agents write {{metric:ARTIFACT_ID:FIELD}} or {{metric:ARTIFACT_ID:FIELD|FORMAT}}; render() fills
each from a MetricStore. FORMAT is a format spec only (e.g. .2f, .1%, ,d); a placeholder never
computes. Any placeholder that cannot be resolved fails the whole render (fail closed).

check_numerals() then fails every digit run in the final text that is not:
  * inside a rendered placeholder,
  * part of a listed identifier (criterion and evidence IDs, versions, run IDs, SHAs),
  * part of an ISO date (YYYY-MM-DD), or
  * inside a "double-quoted" passage copied verbatim from an input (at least MIN_QUOTE_CHARS
    long, containing letters, matched on word boundaries).
"""
from dataclasses import dataclass
import math
import re

PLACEHOLDER = re.compile(r"\{\{metric:([A-Za-z0-9_.@/:-]+):([A-Za-z0-9_.-]+)(?:\|([^{}|]{1,12}))?\}\}")
FORMAT_SPEC = re.compile(r"^,?(\.\d{1,8})?[f%e]?$|^,?d$")
NUMERAL = re.compile(r"\d+(?:[.,]\d+)*")
ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
QUOTED = re.compile(r"\"([^\"\n]{1,2000})\"")
TOKEN = re.compile(r"[A-Za-z0-9_.@/-]+")


class RenderError(ValueError):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = tuple(errors)


@dataclass(frozen=True)
class RenderedNumber:
    start: int
    end: int
    artifact_id: str
    field: str
    value: object
    text: str


@dataclass(frozen=True)
class Rendered:
    text: str
    numbers: tuple      # RenderedNumber, positions in the rendered text


def resolve(store, artifact_id, field):
    """The value at a dotted field path of a stored artifact. Raises KeyError when missing."""
    value = store.get(artifact_id)
    if value is None:
        raise KeyError(f"{artifact_id}: no such artifact")
    for part in field.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise KeyError(f"{artifact_id}:{field}: no field {part!r}")
    return value


def format_value(value, spec):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{value!r} is not a finite number")
    if spec is None:
        return repr(value) if isinstance(value, float) else str(value)
    if not FORMAT_SPEC.match(spec):
        raise ValueError(f"format {spec!r} is not allowed (format only, no arithmetic)")
    if spec.endswith("d") and not isinstance(value, int):
        raise ValueError(f"format {spec!r} needs an integer")
    return format(value, spec)


def render(text, store):
    """Fill every placeholder. Raises RenderError listing every unresolved placeholder."""
    out, numbers, errors, last = [], [], [], 0
    for match in PLACEHOLDER.finditer(text):
        artifact_id, field, spec = match.groups()
        try:
            value = resolve(store, artifact_id, field)
            shown = format_value(value, spec)
        except (KeyError, ValueError) as exc:
            errors.append(f"{match.group(0)}: {exc.args[0] if exc.args else exc}")
            continue
        out.append(text[last:match.start()])
        start = sum(len(part) for part in out)
        out.append(shown)
        numbers.append(RenderedNumber(start, start + len(shown), artifact_id, field, value, shown))
        last = match.end()
    if errors:
        raise RenderError(errors)
    out.append(text[last:])
    rendered = "".join(out)
    if "{{" in rendered or "}}" in rendered:
        raise RenderError(["malformed placeholder left in the text"])
    return Rendered(rendered, tuple(numbers))


MIN_QUOTE_CHARS = 12


def _quoted_from_input(quote, inputs):
    """A quote is exempt only if it is a real passage (some letters, MIN_QUOTE_CHARS or more)
    found in an input on word boundaries, so a bare "15" cannot borrow digits from "2015"."""
    if len(quote) < MIN_QUOTE_CHARS or not any(c.isalpha() for c in quote):
        return False
    pattern = re.compile(r"(?<![\w.])" + re.escape(quote) + r"(?![\w])")
    return any(pattern.search(source) for source in inputs)


@dataclass(frozen=True)
class Untraced:
    text: str
    start: int


def check_numerals(rendered, *, identifiers=(), inputs=()):
    """Digit runs in rendered.text that trace to nothing. Empty list means every number traces.

    identifiers: tokens allowed to contain digits (IDs, versions, SHAs).
    inputs: texts the agent was given; a "quoted" passage copied verbatim from one is exempt.
    """
    text = rendered.text
    covered = [False] * len(text)

    def cover(start, end):
        for i in range(start, end):
            covered[i] = True

    for number in rendered.numbers:
        cover(number.start, number.end)
    for match in ISO_DATE.finditer(text):
        cover(match.start(), match.end())
    allowed = {str(i) for i in identifiers}
    for match in TOKEN.finditer(text):
        if match.group(0).strip(".") in allowed:
            cover(match.start(), match.end())
    for match in QUOTED.finditer(text):
        if _quoted_from_input(match.group(1), inputs):
            cover(match.start(1), match.end(1))
    return [Untraced(m.group(0), m.start()) for m in NUMERAL.finditer(text)
            if not all(covered[m.start():m.end()])]


def provenance(rendered, untraced):
    """(traced, total) digit runs, for scoring."""
    total = len(rendered.numbers) + len(untraced)
    return len(rendered.numbers), total
