"""Phase 10 with a real model: the recorded Claude run of the coding objectives replays offline (real
sandbox and conformance suite, no API call) and scores exactly as it did live (demos/claude_coding.py)."""
import json

import pytest

from finquint.orchestration.demos import claude_coding as demo

MODEL = "claude-opus-5-5"


@pytest.fixture(scope="module")
def replayed():
    return json.loads(json.dumps(demo.replay(MODEL)))


def test_replay_matches_the_live_scores(replayed):
    live = json.loads(demo.paths(MODEL)[2].read_text(encoding="utf-8"))
    assert live["run"]["complete"] and live["run"]["usd"] <= live["run"]["max_usd"]
    assert demo.comparable(replayed) == demo.comparable(live)


def test_claude_meets_every_coding_criterion(replayed):
    s = replayed["summary"]
    assert s["criteria_passed"] == s["criteria_total"]
    assert s["host_executions_before_merge"] == 0 and s["prs_from_failed_checks"] == 0 and s["unapproved_merges"] == 0


def test_claude_templates_pass_conformance_and_review_then_plan(replayed):
    for name, kind in demo.EXPECT.items():
        if kind != "merged":
            continue
        task = replayed["tasks"][name]
        last = task["detail"]["attempts"][-1]
        assert last["outcome"] == "passed" and all(last["conformance"].values())
        assert task["metrics"]["reviews"] == ["accept"] and task["plan"]["run_route"] == "completed"


def test_claude_escalates_a_two_symbol_strategy_without_writing_code(replayed):
    task = replayed["tasks"]["pairs_spread"]
    assert task["metrics"]["status"] == "escalated" and task["metrics"]["sandbox_runs"] == 0
    assert task["metrics"]["prs_opened"] == 0 and task["detail"]["attempts"][0]["stage"] == "agent"


def test_the_change_is_built_against_a_pinned_package_view():
    service_cls = demo.PinnedCodingService
    assert service_cls.existing_modules(None) == sorted(demo.PINNED_MODULES)
    assert set(demo.PINNED_TEMPLATES) == {"ts_momentum"}
