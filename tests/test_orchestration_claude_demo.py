"""Phase 7 step 2: the recorded real-Claude report run replays offline (no key, no network, no SDK)."""
import json

import pytest

from finquint.orchestration.demos import claude_report
from finquint.orchestration.models import ReplayPlug

TASK, MODEL = "report_bond_risk", "claude-opus-5-5"


def recorded_scores():
    path = claude_report.recording_path(TASK, MODEL).with_suffix(".scores.json")
    return json.loads(path.read_text(encoding="utf-8"))


def test_replay_reproduces_the_recorded_live_scores():
    scores = claude_report.replay(TASK, MODEL)
    assert scores == recorded_scores()
    assert scores["status"] == "ok" and scores["model"] == MODEL
    assert scores["numbers_reported"] == scores["numbers_traced"] == scores["criteria"] == 4
    assert scores["invented_reported"] == 0 and scores["wrong_status"] == 0


def test_live_run_needed_one_provenance_repair():
    """Claude's first reply typed convention numbers (5y, per 100, 1 basis point, DV01); the
    numeral post-check sent it back once and the second reply passed."""
    document = json.loads(claude_report.recording_path(TASK, MODEL).read_text(encoding="utf-8"))
    assert recorded_scores()["model_calls"] == 2 == len(document["replies"])
    assert document["plug"] == f"claude:{MODEL}"


def test_a_changed_request_misses_the_recording_and_escalates():
    replay = ReplayPlug.load(claude_report.recording_path(TASK, MODEL))
    scores = claude_report.run_report("report_backtest", replay)
    assert scores["status"] == "escalated" and scores["numbers_reported"] == 0
    assert replay.served == []


def test_only_clean_report_tasks_are_offered_to_a_real_model():
    assert sorted(claude_report.CLEAN_TASKS) == ["report_backtest", "report_bond_risk", "report_strategy_change"]
    with pytest.raises(KeyError):
        claude_report.run_report("fault_invented_number", None)
