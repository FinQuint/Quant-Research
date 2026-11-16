"""Run the Phase 7 full-stack soak with a real model writing the reports, record it, and score it
with the protected scorer from durable state alone.

    # live (costs money; needs `pip install anthropic` and ANTHROPIC_API_KEY); stops at --max-usd
    python -m finquint.orchestration.demos.claude_soak --record --max-usd 4
    # offline, from the saved recording
    python -m finquint.orchestration.demos.claude_soak

The campaign is the protected evals.soak.Campaign, unchanged except for its reporter: every report
the host writes (one per hourly routine run, plus the evidence reports) goes to the given plug. The
"model invents a figure" fault (hour 29) keeps the campaign's scripted inventor, because a real
model does not invent on demand; the stale-claim report (hour 61) escalates before any model call.
Scoring is evals.soak.score() and grade() on the run database and the remote's delivery log.

Each report reply is recorded (KeyedRecorder, keyed by request digest with the normalized fallback
for last-digit float noise), so the replay reruns the whole campaign without the API.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

from ..evals import soak
from ..models.core import ModelCaller
from ..models.replay import ReplayPlug
from .claude_report import RECORDINGS, KeyedRecorder, TolerantReplay
from .claude_sweep import BudgetedPlug, BudgetExceeded

NAME = "soak"
DEFAULT_MAX_USD = 4.0      # (tune) hard cap for one live soak


class ModelCampaign(soak.Campaign):
    """The protected campaign with `plug` as its reporter (the inventor stays scripted)."""

    def __init__(self, directory, policy, plug, **kw):
        self._model = ModelCaller(plug)
        super().__init__(directory, policy, **kw)

    def _open(self):
        self.reporter = self._model        # every Stack, including the one after the crash, uses it
        return super()._open()


def report_summary(directory):
    """Report outcomes from the run database: status counts and model calls (durable state)."""
    db = sqlite3.connect(Path(directory) / "research.sqlite")
    try:
        rows = db.execute("SELECT report_id, status, model, model_calls, problems FROM host_reports "
                          "ORDER BY seq").fetchall()
    finally:
        db.close()
    by_model = [r for r in rows if r[2] and not r[2].startswith("simulated")]
    return {"reports": len(rows),
            "ok": sum(r[1] == "ok" for r in rows), "escalated": sum(r[1] == "escalated" for r in rows),
            "model_reports": len(by_model),
            "model_ok_first_call": sum(r[1] == "ok" and r[3] == 1 for r in by_model),
            "model_ok_after_repair": sum(r[1] == "ok" and r[3] > 1 for r in by_model),
            "model_escalated": [{"report_id": r[0], "problems": json.loads(r[4])} for r in by_model
                                if r[1] == "escalated"]}


def _close(campaign):
    for closer in (campaign.stack.close, campaign.remote.close):
        try:
            closer()
        except Exception:          # already closed, or the failure left it unusable
            pass


def run(plug, *, directory=None, policy=None):
    """The protected run_soak with the model as reporter. Returns the scored document."""
    policy = policy or soak.SoakPolicy.from_file()

    def go(path):
        campaign = ModelCampaign(path, policy, plug)
        try:
            campaign.run()
        except BaseException:      # a stop or crash mid-campaign: release the databases first
            _close(campaign)
            raise
        document = soak.score(path, policy)
        document["reports"] = report_summary(path)
        return document

    if directory is not None:
        document = go(directory)
    else:
        with tempfile.TemporaryDirectory(prefix="finquint-claude-soak-") as tmp:
            document = go(tmp)
    document["grade"] = soak.grade(document, policy)
    return document


def paths(model, root=RECORDINGS):
    base = Path(root) / model / f"{NAME}.json"
    return base, base.with_suffix(".keys.json"), base.with_suffix(".scores.json")


def record(model, *, max_usd=DEFAULT_MAX_USD, plug=None, root=RECORDINGS, **plug_options):
    if plug is None:
        from ..models.claude import ClaudePlug   # optional install
        plug = ClaudePlug(model, context_window=soak.PLUG["context_window"], **plug_options)
    budget = BudgetedPlug(plug, model=model, max_usd=max_usd)
    recorder = KeyedRecorder(budget)
    recording, keys, scores = paths(model, root)
    recording.parent.mkdir(parents=True, exist_ok=True)
    try:
        document = run(recorder)
        complete = True
    except BudgetExceeded as exc:
        document, complete = {"stopped": str(exc)}, False
    finally:   # keep what was paid for, even on a stop or crash
        recorder.save(recording)
        keys.write_text(json.dumps(recorder.keys, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    document["run"] = {"complete": complete, "api_calls": budget.calls, "usd": round(budget.spent, 4),
                       "max_usd": max_usd}
    scores.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return document


def replay(model, root=RECORDINGS):
    recording, keys, _ = paths(model, root)
    plug = TolerantReplay(ReplayPlug.load(recording), json.loads(keys.read_text(encoding="utf-8")))
    return run(plug)


def comparable(document):
    """The scored parts of a document (run cost metadata dropped)."""
    return {k: v for k, v in document.items() if k not in ("run",)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="claude-opus-5-5")
    parser.add_argument("--record", action="store_true", help="call the real API (costs money) and save a recording")
    parser.add_argument("--max-usd", type=float, default=DEFAULT_MAX_USD)
    args = parser.parse_args(argv)
    document = record(args.model, max_usd=args.max_usd) if args.record else replay(args.model)
    summary = {k: document[k] for k in ("grade", "reports", "run", "stopped") if k in document}
    json.dump(summary, sys.stdout, indent=1, sort_keys=True)
    print()
    return 0 if "stopped" not in document and document["grade"]["passed"] == document["grade"]["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
