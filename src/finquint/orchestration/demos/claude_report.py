"""Run a Phase 6 report golden task with a real model, record it, and score it like the simulated run.

    # live (costs money; needs `pip install anthropic` and ANTHROPIC_API_KEY)
    python -m finquint.orchestration.demos.claude_report --task report_bond_risk --record
    # offline, from the saved recording
    python -m finquint.orchestration.demos.claude_report --task report_bond_risk

Only the clean report tasks apply: the fault tasks script a simulated model's misbehavior, which a
real model does not reproduce on demand. The task setup, request, report pipeline
(knowledge.reporting.write_report: placeholders, numeral post-check, one repair, ESCALATE) and
scoring are the protected Phase 6 evaluator's own, imported unchanged; only the plug differs.

Each recording keeps the model's replies keyed by request digest, so the replay is the exact run.
A second index (<task>.keys.json) maps a normalized digest, with long decimals rounded to 12
significant digits, to the exact one. The bond analytics differ in the last float digit across
Python versions (3.12 made sum() of floats compensated), which changes the exact digest; the
replay falls back to the normalized key so the recording replays on every supported Python.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
import tempfile

from ..evals import numbers
from ..knowledge.reporting import metric_segment, write_report
from ..models.core import ModelCaller
from ..models.replay import RecordingPlug, ReplayMiss, ReplayPlug

RECORDINGS = Path(__file__).resolve().parent / "recordings"
CLEAN_TASKS = {t.name: t for t in numbers.TASKS if t.expect == "ok" and t.mode == "placeholder" and not t.invent}
# USD per million tokens (input, output), for the cost line only (tune when prices change)
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}


_LONG_DECIMAL = re.compile(r"\d+\.\d{8,}")
NORMALIZED_DIGITS = 12   # significant digits kept in a normalized key (tune)


def normalized_key(request):
    """request.digest() with long decimals rounded, so last-digit float noise maps to the same key."""
    def norm(text):
        return _LONG_DECIMAL.sub(lambda m: format(float(m.group()), f".{NORMALIZED_DIGITS}g"), text)
    body = {"run_id": request.run_id, "task_id": request.task_id, "max_output_tokens": request.max_output_tokens,
            "output_schema": request.output_schema,
            "segments": [[s.role.value, norm(s.content), s.stable, list(s.ids)] for s in request.segments]}
    return sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class KeyedRecorder(RecordingPlug):
    """RecordingPlug that also keeps normalized key -> exact digest."""

    def __init__(self, plug):
        super().__init__(plug)
        self.keys = {}

    def complete(self, request):
        reply = super().complete(request)
        self.keys[normalized_key(request)] = request.digest()
        return reply


class TolerantReplay(ReplayPlug):
    """ReplayPlug that falls back to the normalized key when the exact digest is not recorded."""

    def __init__(self, base, keys):
        super().__init__(base.name, base.recordings, capabilities=base.capabilities)
        self.keys = dict(keys)

    def complete(self, request):
        key = request.digest()
        if key not in self.recordings:
            key = self.keys.get(normalized_key(request), key)
        if key not in self.recordings:
            raise ReplayMiss(f"no recorded reply for request {request.digest()[:12]}")
        return super().complete(_Pinned(request, key))


class _Pinned:
    """A request whose digest() is pinned to a recorded key (ReplayPlug looks replies up by digest)."""

    def __init__(self, request, key):
        self._request, self._key = request, key

    def digest(self):
        return self._key


def run_report(task_name, plug):
    """Score one clean report task with `plug`, exactly as numbers.run_task scores the provenance pipeline."""
    task = CLEAN_TASKS[task_name]
    with tempfile.TemporaryDirectory(prefix="finquint-demo-") as directory:
        research, store, refs, card = task.setup(directory)
        try:
            request = numbers._request(card, metric_segment(store, refs))
            result = write_report(ModelCaller(plug), request, metrics=store)
            reported = len(result.numbers) if result.status == "ok" else 0
            return {"status": result.status, "model": result.model, "numbers_reported": reported,
                    "numbers_traced": reported,
                    "invented_reported": 0 if result.status != "ok" else numbers._invented(result.numbers, store, refs),
                    "wrong_status": int(result.status != task.expect), "model_calls": result.model_calls,
                    "criteria": len(card["criteria"]), "problems": list(result.problems), "report": result.text}
        finally:
            research.close()


def recording_path(task_name, model):
    return RECORDINGS / model / f"{task_name}.json"


def keys_path(task_name, model):
    return recording_path(task_name, model).with_suffix(".keys.json")


def load_replay(task_name, model):
    base = ReplayPlug.load(recording_path(task_name, model))
    keys = json.loads(keys_path(task_name, model).read_text(encoding="utf-8"))
    return TolerantReplay(base, keys)


def usage_and_cost(recording, model):
    replies = recording["replies"].values()
    tokens_in = sum(r["usage"]["input_tokens"] for r in replies)
    tokens_out = sum(r["usage"]["output_tokens"] for r in replies)
    price_in, price_out = PRICES.get(model, (0.0, 0.0))
    return {"input_tokens": tokens_in, "output_tokens": tokens_out,
            "usd": round(tokens_in / 1e6 * price_in + tokens_out / 1e6 * price_out, 4)}


def record(task_name, model, **plug_options):
    from ..models.claude import ClaudePlug   # optional install
    recorder = KeyedRecorder(ClaudePlug(model, **plug_options))
    scores = run_report(task_name, recorder)
    path = recording_path(task_name, model)
    path.parent.mkdir(parents=True, exist_ok=True)
    recorder.save(path)
    keys_path(task_name, model).write_text(json.dumps(recorder.keys, indent=1, sort_keys=True) + "\n",
                                           encoding="utf-8")
    document = json.loads(path.read_text(encoding="utf-8"))
    scores["usage"] = usage_and_cost(document, model)
    path.with_suffix(".scores.json").write_text(json.dumps(scores, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return scores


def replay(task_name, model):
    path = recording_path(task_name, model)
    scores = run_report(task_name, load_replay(task_name, model))
    scores["usage"] = usage_and_cost(json.loads(path.read_text(encoding="utf-8")), model)
    return scores


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--task", choices=sorted(CLEAN_TASKS), default="report_bond_risk")
    parser.add_argument("--model", default="claude-opus-5-5")
    parser.add_argument("--record", action="store_true", help="call the real API (costs money) and save a recording")
    args = parser.parse_args(argv)
    scores = record(args.task, args.model) if args.record else replay(args.task, args.model)
    json.dump(scores, sys.stdout, indent=1, sort_keys=True)
    print()
    return 0 if scores["wrong_status"] == 0 and scores["invented_reported"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
