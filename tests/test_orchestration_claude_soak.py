"""Phase 7: the full-stack soak with a real model as reporter, checked offline with the simulated
reporter (the live Claude recording is replayed in its own test)."""
import pytest

from finquint.orchestration.demos import claude_soak
from finquint.orchestration.evals import soak
from finquint.orchestration.models.simulated import SimulatedReporter


def simulated():
    return SimulatedReporter(soak.PLUG["name"], context_window=soak.PLUG["context_window"])


@pytest.fixture(scope="module")
def protected():
    return soak.run_soak()


def test_model_campaign_with_the_simulated_reporter_scores_like_the_protected_soak(protected):
    document = claude_soak.run(simulated())
    for key, value in protected.items():
        if key not in ("policy", "grade"):
            assert document[key] == value, key
    assert document["grade"] == protected["grade"]
    assert document["reports"]["reports"] == 74 and document["reports"]["escalated"] == 2


def test_the_invented_number_fault_keeps_the_scripted_inventor(tmp_path):
    policy = soak.SoakPolicy.from_file()
    campaign = claude_soak.ModelCampaign(tmp_path, policy, simulated())
    try:
        assert campaign.reporter is campaign._model and campaign.stack.reporter is campaign._model
        assert campaign.inventor is not campaign._model
    finally:
        campaign.stack.close()
        campaign.remote.close()


def test_record_then_replay_round_trip_offline(tmp_path):
    recorded = claude_soak.record("claude-opus-5-5", plug=simulated(), root=tmp_path, max_usd=100)
    # 71 routine reports and the hour-7 evidence report; the invented-number report stays scripted
    assert recorded["run"]["complete"] and recorded["run"]["api_calls"] == 72
    replayed = claude_soak.replay("claude-opus-5-5", root=tmp_path)
    assert claude_soak.comparable(replayed) == claude_soak.comparable(recorded)


def test_a_stopped_soak_keeps_what_was_paid_for(tmp_path):
    result = claude_soak.record("claude-opus-5-5", plug=simulated(), root=tmp_path, max_usd=0.0)
    assert not result["run"]["complete"] and "stopped" in result
    recording, keys, scores = claude_soak.paths("claude-opus-5-5", tmp_path)
    assert recording.exists() and keys.exists() and scores.exists()


def test_recorded_claude_soak_replays_offline_to_its_saved_scores():
    """Live run, claude-opus-5-5 as reporter, shared report prompt (2026-11-15, 140 calls, $1.60),
    re-scored after the numeral check matched identifiers ignoring case: 17/17, every Claude report
    passes on its first call, both fault reports escalate. In the live run 81 repairs were for the
    "01" in the label "DV01"; the replies are unchanged, the unused repair replies were dropped."""
    import json
    _, _, scores = claude_soak.paths("claude-opus-5-5")
    saved = json.loads(scores.read_text(encoding="utf-8"))
    replayed = claude_soak.replay("claude-opus-5-5")
    assert claude_soak.comparable(replayed) == claude_soak.comparable(saved)
    assert replayed["grade"]["passed"] == replayed["grade"]["total"] == 17
    assert replayed["metrics"]["untraced_numbers"] == 0 and replayed["metrics"]["traced_share"] == 1.0
    assert replayed["metrics"]["routine_report_escalations"] == 0
    assert replayed["facts"]["fault_reports"] == {"report-soak-r29": "escalated", "report-soak-e61": "escalated"}
    assert replayed["reports"]["model_ok_first_call"] == 72 and replayed["reports"]["model_ok_after_repair"] == 0
    assert saved["run"]["live_reports_ok_first_call"] == 4 and "rescored" in saved["run"]
