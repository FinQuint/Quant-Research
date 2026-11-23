"""Phase 8 with a real model: the recorded Claude run of the recovery tasks replays offline and scores
exactly as it did live (demos/claude_recovery.py; no API call in CI)."""
import json

import pytest

from finquint.orchestration.demos import claude_recovery as demo

MODEL = "claude-opus-5-5"
RECORDING, KEYS, SCORES = demo.paths(MODEL)


@pytest.fixture(scope="module")
def replayed():
    return demo.replay(MODEL)


def test_replay_matches_the_live_scores(replayed):
    live = json.loads(SCORES.read_text(encoding="utf-8"))
    assert live["run"]["complete"] and live["run"]["usd"] <= live["run"]["max_usd"]
    assert demo.comparable(replayed) == demo.comparable(live)


def test_claude_meets_every_recovery_criterion(replayed):
    s = replayed["summary"]
    assert s["criteria_passed"] == s["criteria_total"]
    assert s["llm_calls_policy_path"] == 0 and s["accepted_drift_or_unfixable"] == 0


def test_claude_refuses_to_change_the_question(replayed):
    drift = replayed["tasks"]["gaming_goal_drift"]
    assert drift["metrics"]["completed_children"] == 0 and drift["metrics"]["route"] == "escalated"
