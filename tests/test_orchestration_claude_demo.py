"""Phase 7 step 2: the recorded real-Claude report run replays offline (no key, no network, no SDK)."""
import json
import math

import pytest

from finquint.orchestration.demos import claude_report
from finquint.orchestration.models import ReplayPlug

TASK, MODEL = "report_bond_risk", "claude-opus-5-5"
# task -> (criteria, model calls in the live run)
RECORDED = {"report_bond_risk": (4, 1), "report_backtest": (3, 1), "report_strategy_change": (3, 1)}


def recorded_scores(task=TASK):
    path = claude_report.recording_path(task, MODEL).with_suffix(".scores.json")
    return json.loads(path.read_text(encoding="utf-8"))


def comparable(scores):
    """Scores with the report's long decimals rounded: the rendered values carry the running
    Python's last-digit float noise (3.10/3.11 vs 3.12), the recorded ones 3.12's."""
    report = claude_report._LONG_DECIMAL.sub(lambda m: format(float(m.group()), ".12g"), scores["report"])
    return {**scores, "report": report}


@pytest.mark.parametrize("task", sorted(RECORDED))
def test_replay_reproduces_the_recorded_live_scores(task):
    criteria, calls = RECORDED[task]
    scores = claude_report.replay(task, MODEL)
    assert comparable(scores) == comparable(recorded_scores(task))
    assert scores["status"] == "ok" and scores["model"] == MODEL and scores["model_calls"] == calls
    assert scores["numbers_reported"] == scores["numbers_traced"] == scores["criteria"] == criteria
    assert scores["invented_reported"] == 0 and scores["wrong_status"] == 0


def test_every_clean_report_task_has_a_claude_recording():
    assert sorted(RECORDED) == sorted(claude_report.CLEAN_TASKS)


def test_live_run_passes_on_the_first_call():
    """With the shared report prompt Claude spells out conventions ("five-year", "one hundred"), and
    the section label "DV01" is the identifier "dv01" (matched ignoring case), so the first reply
    passes. Before those fixes this task needed one provenance repair."""
    document = json.loads(claude_report.recording_path(TASK, MODEL).read_text(encoding="utf-8"))
    assert recorded_scores()["model_calls"] == 1 == len(document["replies"])
    assert "DV01" in recorded_scores()["report"] or "dollar value" in recorded_scores()["report"]
    assert document["plug"] == f"claude:{MODEL}"


def test_a_changed_request_misses_the_recording_and_escalates():
    replay = ReplayPlug.load(claude_report.recording_path(TASK, MODEL))
    scores = claude_report.run_report("report_backtest", replay)    # bond replies, backtest request
    assert scores["status"] == "escalated" and scores["numbers_reported"] == 0
    assert replay.served == []


def test_only_clean_report_tasks_are_offered_to_a_real_model():
    assert sorted(claude_report.CLEAN_TASKS) == ["report_backtest", "report_bond_risk", "report_strategy_change"]
    with pytest.raises(KeyError):
        claude_report.run_report("fault_invented_number", None)


def _last_digit_noise(monkeypatch):
    """Simulate another Python's float summation: the bond price differs in its last digits."""
    real = claude_report.metric_segment

    def noisy(store, refs):
        segment = real(store, refs)
        head, value = segment.content.split("\n", 1)[0].rsplit(" ", 1)
        nudged = math.nextafter(math.nextafter(float(value), math.inf), math.inf)   # two ulps up
        content = segment.content.replace(f"{head} {value}", f"{head} {nudged!r}", 1)
        assert content != segment.content and "bond_price" in head
        return type(segment)(segment.role, content, segment.stable, segment.ids)
    monkeypatch.setattr(claude_report, "metric_segment", noisy)


def test_replay_tolerates_last_digit_float_noise_across_python_versions(monkeypatch):
    _last_digit_noise(monkeypatch)
    exact_only = claude_report.run_report(TASK, ReplayPlug.load(claude_report.recording_path(TASK, MODEL)))
    assert exact_only["status"] == "escalated"          # the CI failure on 3.10/3.11 before the fix
    assert comparable(claude_report.replay(TASK, MODEL)) == comparable(recorded_scores())


def test_normalized_key_ignores_float_noise_but_not_real_changes():
    from finquint.orchestration.models import ModelRequest, Segment, SegmentRole
    schema = {"type": "object"}

    def req(value):
        return ModelRequest("r", "t", (Segment(SegmentRole.EVIDENCE, f"[metric a field=x] {value}"),), schema)
    base = req("104.4912925031211")
    assert claude_report.normalized_key(base) == claude_report.normalized_key(req("104.49129250312108"))
    assert base.digest() != req("104.49129250312108").digest()
    assert claude_report.normalized_key(base) != claude_report.normalized_key(req("104.4912935031211"))
    assert claude_report.normalized_key(req("1.5")) != claude_report.normalized_key(req("1.6"))
