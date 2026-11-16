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
    # distinct requests with the config's switches on (37 with them off: prioritized rebuilds
    # differ from each other, so fewer repeat, though each run makes fewer calls)
    assert recorded["run"]["complete"] and recorded["run"]["api_calls"] == 57
    assert recorded["run"]["switches"] == sweep.REBUILD
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


def test_exact_count_mode_records_counts_and_replays_without_the_api(tmp_path):
    """The simulated plug counts exactly, like ClaudePlug(exact_tokens=True): its counts are
    recorded and the replay rebuilds identical requests from them alone."""
    recorded = sweep.record("claude-opus-5-5", plug=cx.plug(), root=tmp_path, max_usd=100, exact=True)
    assert recorded["run"]["exact_tokens"] and recorded["run"]["token_counts"] > 0
    assert sweep.counts_path("claude-opus-5-5", tmp_path).exists()
    replayed = sweep.replay("claude-opus-5-5", root=tmp_path, exact=True)
    for key in ("picked", "sweeps", "tasks", "naive_sensitivity", "totals"):
        assert replayed[key] == recorded[key], key


def test_recorded_counter_fails_loudly_on_an_uncounted_text():
    counter = sweep.recorded_counter({})
    with pytest.raises(KeyError):
        counter("never counted")


def test_claude_plug_message_overhead_is_opt_in_and_measured_once():
    from types import SimpleNamespace
    from finquint.orchestration.models.claude import ClaudePlug

    class Client:
        def __init__(self):
            self.calls, self.messages = 0, self

        def count_tokens(self, **params):
            self.calls += 1
            return SimpleNamespace(input_tokens=8 + len(params["messages"][0]["content"].split()))
    raw = ClaudePlug(client=Client(), exact_tokens=True)
    assert raw.count_tokens("a b c") == 11                     # default: raw count, overcounts
    client = Client()
    net = ClaudePlug(client=client, exact_tokens=True, message_overhead=None)
    assert net.count_tokens("a b c") == 3 and net.count_tokens("a") == 1
    assert client.calls == 2                                   # "a" measured once, then cached
    assert ClaudePlug(client=Client(), exact_tokens=True, message_overhead=8).count_tokens("a b") == 2
    with pytest.raises(ValueError):
        ClaudePlug(client=Client(), message_overhead=-1)


def test_recorded_exact_count_claude_sweep_replays_offline():
    """Live run with Claude's exact token counts (recorded) at the 2048-token window: every grid
    setting has quality failures, because real tokens run ~1.7x the chars/4 estimate and some
    criteria's evidence never fits. Picks here are not usable for tuning (see docs/EVALS.md)."""
    _, _, scores = sweep.paths("claude-opus-5-5", exact=True)
    saved = json.loads(scores.read_text(encoding="utf-8"))
    assert saved["run"]["exact_tokens"] and saved["run"]["complete"]
    replayed = sweep.replay("claude-opus-5-5", exact=True)
    for key in ("picked", "sweeps", "tasks", "naive_sensitivity", "totals"):
        assert replayed[key] == saved[key], key
    fails = lambda r: r["forgotten_criteria"] + r["citation_failures"] + r["wrong_status"]
    assert min(map(fails, replayed["sweeps"]["context"])) > 0


def test_switches_on_run_pays_only_for_requests_the_off_recording_lacks(tmp_path):
    off = sweep.record("claude-opus-5-5", plug=cx.plug(), root=tmp_path, max_usd=100, exact=True)
    on = sweep.record("claude-opus-5-5", plug=cx.plug(), root=tmp_path, max_usd=100, exact=True, rebuild=True,
                      cache={"exact": True, "window": sweep.WINDOW, "rebuild": False})
    assert on["run"]["cache_hits"] > 0 and on["run"]["api_calls"] < off["run"]["api_calls"]
    assert on["run"]["rebuild_switches"] == sweep.REBUILD
    fresh = sweep.run_all(cx.plug(), sweep.REBUILD)
    for key in ("picked", "sweeps", "tasks", "totals"):
        assert on[key] == fresh[key], key                    # the cache changes cost, never results
    replayed = sweep.replay("claude-opus-5-5", root=tmp_path, exact=True, rebuild=True)
    assert replayed["sweeps"] == on["sweeps"]


def test_variant_names_keep_the_original_recordings():
    assert sweep.variant() == "context_sweep" and sweep.variant(exact=True) == "context_sweep_exact"
    assert sweep.variant(exact=True, window=2943, rebuild=True) == "context_sweep_exact_w2943_rebuild"
    assert sweep.SCALED_WINDOW == 2943


@pytest.mark.parametrize("rebuild", [False, True])
def test_recorded_scaled_window_claude_sweeps_replay_offline(rebuild):
    _, _, scores = sweep.paths("claude-opus-5-5", exact=True, window=sweep.SCALED_WINDOW, rebuild=rebuild)
    saved = json.loads(scores.read_text(encoding="utf-8"))
    assert saved["run"]["complete"] and saved["run"]["window"] == sweep.SCALED_WINDOW
    replayed = sweep.replay("claude-opus-5-5", exact=True, window=sweep.SCALED_WINDOW, rebuild=rebuild)
    for key in ("picked", "sweeps", "tasks", "naive_sensitivity", "totals"):
        assert replayed[key] == saved[key], key


def test_scaled_window_claude_picks_match_the_config_and_switches_cut_failures_and_calls():
    """At the window scaled to Claude's tokens, Claude picks the current config's values; the
    opt-in rebuild switches cut grid failures and calls without changing picks or escalations."""
    load = lambda rebuild: json.loads(sweep.paths("claude-opus-5-5", exact=True, window=sweep.SCALED_WINDOW,
                                                  rebuild=rebuild)[2].read_text(encoding="utf-8"))
    off, on = load(False), load(True)
    assert off["picked"] == on["picked"] == cx.load_baseline()["picked"]
    fails = lambda r: r["forgotten_criteria"] + r["citation_failures"] + r["wrong_status"]
    assert sum(map(fails, on["sweeps"]["context"])) < sum(map(fails, off["sweeps"]["context"]))
    assert sum(r["model_calls"] for r in on["sweeps"]["context"]) < sum(r["model_calls"] for r in off["sweeps"]["context"])
    assert on["sweeps"]["rebuilds"] == off["sweeps"]["rebuilds"]
    assert off["totals"]["builder"]["forgotten_criteria"] == 0 < off["totals"]["naive"]["forgotten_criteria"]
