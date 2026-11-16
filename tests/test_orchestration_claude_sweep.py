"""Phase 7 step 2: the real-model context sweep runner, checked offline with the simulated plug."""
import json

import pytest

from finquint.orchestration.demos import claude_sweep as sweep
from finquint.orchestration.evals import context as cx
from finquint.orchestration.models import ModelCaller, ModelEscalation, ModelReply, ModelRequest, Segment, SegmentRole, Usage
from finquint.orchestration.models.socket import Capabilities, SOCKET_VERSION


@pytest.fixture(scope="module")
def simulated_run():
    return sweep.run_all(cx.plug())


def test_runner_with_the_simulated_plug_reproduces_the_protected_baseline(simulated_run):
    baseline = cx.load_baseline()
    for key in ("picked", "sweeps", "tasks", "naive_sensitivity", "totals"):
        assert simulated_run[key] == baseline[key], key
    assert sweep.compare(simulated_run)["same_picks"]


def test_window_stays_at_the_simulated_plugs_so_evidence_overflows():
    assert sweep.WINDOW == cx.PLUG["context_window"] == 2048


class Priced:
    """A plug that reports fixed usage, to drive the budget."""
    socket_version = SOCKET_VERSION

    def __init__(self, tokens_in=100_000, tokens_out=0):
        self.name, self.capabilities = "priced", Capabilities(context_window=4096)
        self.usage, self.calls = Usage(tokens_in, tokens_out, exact=True), 0

    def complete(self, request):
        self.calls += 1
        return ModelReply('{"answer": "x"}', "priced", self.usage)


def _request(text="q"):
    return ModelRequest("r", "t", (Segment(SegmentRole.VOLATILE, text),),
                        {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}})


def test_budget_stops_before_the_cap_and_escapes_the_socket_core():
    inner = Priced()                       # each call: 100k input tokens at $4/M = $0.40
    plug = sweep.BudgetedPlug(inner, model="claude-opus-5-5", max_usd=1.0, reserve=0.05)
    caller = ModelCaller(plug)
    caller.call(_request("a")); caller.call(_request("b"))
    assert plug.spent == pytest.approx(0.80)
    with pytest.raises(sweep.BudgetExceeded):   # 0.80 + 0.05 reserve <= 1.0, so one more call runs
        caller.call(_request("c")); caller.call(_request("d"))
    assert inner.calls == 3 and plug.spent <= 1.0 + 0.40
    assert not issubclass(sweep.BudgetExceeded, Exception)


def test_memo_answers_a_repeated_request_without_a_call():
    inner = Priced()
    memo = sweep.MemoPlug(inner)
    first = memo.complete(_request("same"))
    assert memo.complete(_request("same")) is first and inner.calls == 1 and memo.hits == 1
    memo.complete(_request("other"))
    assert inner.calls == 2


def test_record_then_replay_round_trip_offline(tmp_path, simulated_run):
    recorded = sweep.record("claude-opus-5-5", plug=cx.plug(), root=tmp_path, max_usd=100)
    assert recorded["run"]["complete"] and recorded["run"]["api_calls"] == 37
    assert recorded["run"]["memo_hits"] > recorded["run"]["api_calls"]
    replayed = sweep.replay("claude-opus-5-5", root=tmp_path, count_tokens=cx.plug().count_tokens)
    for key in ("picked", "sweeps", "tasks", "naive_sensitivity", "totals"):
        assert replayed[key] == recorded[key] == simulated_run[key], key
    _, _, scores = sweep.paths("claude-opus-5-5", tmp_path)
    assert json.loads(scores.read_text(encoding="utf-8"))["comparison"]["same_picks"]


def test_a_stopped_run_keeps_what_was_paid_for(tmp_path):
    result = sweep.record("claude-opus-5-5", plug=cx.plug(), root=tmp_path, max_usd=0.0)
    assert not result["run"]["complete"] and "stopped" in result and "comparison" not in result
    recording, keys, scores = sweep.paths("claude-opus-5-5", tmp_path)
    assert recording.exists() and keys.exists() and scores.exists()


def test_replay_without_the_recorded_counter_builds_different_requests(tmp_path):
    """Why replay takes count_tokens: the simulated plug counts exactly, the estimate does not."""
    sweep.record("claude-opus-5-5", plug=cx.plug(), root=tmp_path, max_usd=100)
    with pytest.raises(ModelEscalation, match="no recorded reply"):
        sweep.replay("claude-opus-5-5", root=tmp_path)


def test_claude_plug_is_recorded_and_replayed_with_the_same_estimate():
    from finquint.orchestration.models import TokenCounter
    from finquint.orchestration.models.claude import ClaudePlug
    from finquint.orchestration.models.replay import ReplayPlug

    class NoClient:
        messages = None
    live = ClaudePlug(client=NoClient(), context_window=sweep.WINDOW)
    replay = ReplayPlug("replay:x", {}, capabilities=Capabilities(context_window=sweep.WINDOW))
    text = "word " * 300
    assert not TokenCounter(live).exact and not TokenCounter(replay).exact
    assert TokenCounter(live).count(text) == TokenCounter(replay).count(text)


@pytest.fixture(scope="module")
def claude_replay():
    return sweep.replay("claude-opus-5-5")


def test_recorded_claude_sweep_replays_offline_to_its_saved_scores(claude_replay):
    _, _, scores = sweep.paths("claude-opus-5-5")
    saved = json.loads(scores.read_text(encoding="utf-8"))
    assert saved["run"]["complete"] and saved["run"]["usd"] < saved["run"]["max_usd"]
    for key in ("picked", "sweeps", "tasks", "naive_sensitivity", "totals"):
        assert claude_replay[key] == saved[key], key


def test_claude_sweep_result_after_the_citation_version_fix(claude_replay):
    """Builder: no forgotten criteria or citation failures; naive forgets. Picks differ from the
    simulated plug's only in utilization_threshold (0.9 vs 0.8): with the padded token estimate,
    0.8 trims evidence a criterion needs (see docs/EVALS.md)."""
    builder, naive = claude_replay["totals"]["builder"], claude_replay["totals"]["naive"]
    assert builder["forgotten_criteria"] == builder["citation_failures"] == builder["wrong_status"] == 0
    assert naive["forgotten_criteria"] > 0
    assert claude_replay["picked"] == {"utilization_threshold": 0.9, "history_last_n": 16, "max_failed_rebuilds": 2}
    assert cx.load_baseline()["picked"]["utilization_threshold"] == 0.8
