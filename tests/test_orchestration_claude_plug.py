"""Phase 7 step 2: the Claude plug, tested against a fake client (no network, no SDK needed)."""
from types import SimpleNamespace
import json
import sys

import pytest

from finquint.autonomous import RetryableError
from finquint.orchestration.models import (ModelCaller, ModelEscalation, ModelRefused, ModelRequest, ModelUnavailable,
                                           RecordingPlug, ReplayPlug, Segment, SegmentRole, TokenCounter,
                                           check_conformance, check_plug)
from finquint.orchestration.models.claude import DEFAULT_MODEL, ClaudePlug, map_error, schema_instruction

SCHEMA = {"type": "object", "required": ["answer"], "additionalProperties": False,
          "properties": {"answer": {"type": "string", "minLength": 1}}}


def request(max_output_tokens=64):
    return ModelRequest("r1", "t1", (Segment(SegmentRole.SYSTEM, "You are a quant analyst.", stable=True),
                                     Segment(SegmentRole.TASK_CARD, "Task: report duration.", stable=True),
                                     Segment(SegmentRole.EVIDENCE, "[evidence E1 v1] duration 4.2"),
                                     Segment(SegmentRole.VOLATILE, "Answer now.")), SCHEMA, max_output_tokens)


class APIStatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


class APIConnectionError(Exception):
    pass


class APITimeoutError(APIConnectionError):
    pass


def message(text='{"answer": "4.2"}', stop_reason="end_turn", model=DEFAULT_MODEL, cache_read=0, details=None):
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason, stop_details=details, model=model,
        usage=SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=cache_read,
                              cache_creation_input_tokens=None))


class _Stream:
    def __init__(self, item):
        self.item = item

    def __enter__(self):
        if isinstance(self.item, Exception):
            raise self.item
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.item


class FakeClient:
    """Mimics anthropic.Anthropic().messages.stream / count_tokens."""

    def __init__(self, replies=None, count_error=None):
        self.replies = list(replies or [])
        self.calls, self.count_calls = [], []
        self.count_error = count_error
        self.messages = self

    def stream(self, **params):
        self.calls.append(params)
        return _Stream(self.replies.pop(0) if self.replies else message())

    def count_tokens(self, **params):
        self.count_calls.append(params)
        if self.count_error:
            raise self.count_error
        return SimpleNamespace(input_tokens=3 + len(params["messages"][0]["content"]) // 4)


def plug(**kw):
    kw.setdefault("client", FakeClient())
    return ClaudePlug(**kw)


def test_plug_passes_the_socket_conformance_suite():
    outage = lambda: ClaudePlug(client=FakeClient([APIStatusError(529)]))
    assert check_conformance(plug(), request(), outage=outage) == []
    assert check_conformance(plug(exact_tokens=True), request(), outage=outage) == []
    check_plug(plug())


def test_request_mapping_roles_order_cache_and_schema():
    client = FakeClient()
    reply = ClaudePlug(client=client).complete(request())
    params = client.calls[0]
    assert params["model"] == "claude-opus-5-5"
    assert params["max_tokens"] == 64 + 8000
    assert params["output_config"] == {"effort": "high"}
    assert "thinking" not in params and "temperature" not in params
    system, user = params["system"], params["messages"][0]["content"]
    assert [b["text"] for b in system] == ["You are a quant analyst.", schema_instruction(SCHEMA)]
    assert [b["text"] for b in user] == ["Task: report duration.", "[evidence E1 v1] duration 4.2", "Answer now."]
    # one breakpoint, on the last stable segment (the task card), nothing volatile cached
    marked = [b for b in system + user if "cache_control" in b]
    assert marked == [user[0]] and marked[0]["cache_control"] == {"type": "ephemeral"}
    assert params["messages"][0]["role"] == "user" and len(params["messages"]) == 1
    assert json.loads(system[-1]["text"].split("\n", 1)[1]) == SCHEMA
    assert reply.text == '{"answer": "4.2"}'      # thinking blocks are dropped


def test_options_structured_output_no_cache_effort_and_fallbacks():
    client = FakeClient()
    p = ClaudePlug("claude-sonnet-5-5", client=client, structured_output=True, cache=False, effort=None,
                   thinking_headroom=0, server_fallbacks="default")
    p.complete(request())
    params = client.calls[0]
    assert params["model"] == "claude-sonnet-5-5" and params["max_tokens"] == 64
    assert params["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}
    assert not any("cache_control" in b for b in params["system"] + params["messages"][0]["content"])
    assert params["extra_body"] == {"fallbacks": "default"}
    assert params["extra_headers"] == {"anthropic-beta": "server-side-fallback-2026-07-01"}
    assert p.capabilities.native_structured_output and not p.capabilities.prompt_caching
    assert "extra_body" not in ClaudePlug(client=FakeClient()).build_params(request())   # off by default


def test_usage_counts_cached_input_and_reports_the_answering_model():
    client = FakeClient([message(cache_read=900, model="claude-opus-5-5")])
    reply = ClaudePlug(client=client).complete(request())
    assert (reply.usage.input_tokens, reply.usage.output_tokens, reply.usage.exact) == (1000, 20, True)
    assert reply.model == "claude-opus-5-5"


@pytest.mark.parametrize("exc", [APIStatusError(429), APIStatusError(500), APIStatusError(529), APIStatusError(408),
                                 APIConnectionError("down"), APITimeoutError("slow"), TimeoutError("slow")])
def test_outages_become_model_unavailable(exc):
    with pytest.raises(ModelUnavailable):
        plug(client=FakeClient([exc])).complete(request())


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413])
def test_client_errors_become_refusals(status):
    with pytest.raises(ModelRefused):
        plug(client=FakeClient([APIStatusError(status)])).complete(request())


def test_unknown_exceptions_propagate_and_map_error_ignores_them():
    assert map_error(ValueError("bug")) is None
    with pytest.raises(ValueError):
        plug(client=FakeClient([ValueError("bug")])).complete(request())


def test_refusal_stop_reason_escalates_and_is_not_retried_on_a_fallback():
    refused = plug(client=FakeClient([message(stop_reason="refusal", details=SimpleNamespace(category="cyber"))]))
    backup_client = FakeClient()
    with pytest.raises(ModelEscalation, match="cyber"):
        ModelCaller(refused, fallbacks=(ClaudePlug("claude-sonnet-5-5", client=backup_client),)).call(request())
    assert backup_client.calls == []


def test_core_falls_back_on_outage_then_raises_retryable_when_all_are_down():
    backup = ClaudePlug("claude-sonnet-5-5", client=FakeClient([message(model="claude-sonnet-5-5")]))
    result = ModelCaller(plug(client=FakeClient([APIStatusError(529)])), fallbacks=(backup,)).call(request())
    assert result.model == "claude-sonnet-5-5" and result.output == {"answer": "4.2"}
    with pytest.raises(RetryableError):
        ModelCaller(plug(client=FakeClient([APIStatusError(503)]))).call(request())


def test_invalid_json_gets_one_repair_then_escalates():
    client = FakeClient([message("not json"), message('{"answer": "4.2"}')])
    result = ModelCaller(ClaudePlug(client=client)).call(request())
    assert result.repaired and len(client.calls) == 2
    assert "did not match" in client.calls[1]["messages"][0]["content"][-1]["text"]
    with pytest.raises(ModelEscalation):
        ModelCaller(plug(client=FakeClient([message("no"), message("still no")]))).call(request())


def test_truncated_reply_escalates_after_repair():
    cut = message('{"answer": "4', stop_reason="max_tokens")
    with pytest.raises(ModelEscalation):
        ModelCaller(plug(client=FakeClient([cut, cut]))).call(request())


def test_exact_token_count_is_opt_in_cached_and_maps_errors():
    assert not plug().capabilities.exact_token_count
    assert not TokenCounter(plug()).exact
    client = FakeClient()
    p = ClaudePlug(client=client, exact_tokens=True)
    assert TokenCounter(p).exact
    assert p.count_tokens("abcd" * 10) == p.count_tokens("abcd" * 10) == 13
    assert len(client.count_calls) == 1
    assert client.count_calls[0]["model"] == DEFAULT_MODEL
    with pytest.raises(ModelUnavailable):
        ClaudePlug(client=FakeClient(count_error=APIStatusError(429)), exact_tokens=True).count_tokens("x")


def test_record_then_replay_without_calling_the_api(tmp_path):
    client = FakeClient()
    recorder = RecordingPlug(ClaudePlug(client=client))
    first = ModelCaller(recorder).call(request())
    path = tmp_path / "claude.json"
    recorder.save(path)
    replay = ReplayPlug.load(path)
    assert replay.name == "replay:claude:claude-opus-5-5"
    again = ModelCaller(replay).call(request())
    assert again.output == first.output and again.model == first.model
    assert len(client.calls) == 1


def test_constructor_validation_and_missing_sdk(monkeypatch):
    for bad in ({"model": ""}, {"effort": "huge"}, {"thinking_headroom": -1}):
        with pytest.raises(ValueError):
            plug(**bad)
    monkeypatch.setitem(sys.modules, "anthropic", None)
    with pytest.raises(ImportError, match="pip install anthropic"):
        ClaudePlug()


def test_core_and_package_do_not_import_the_sdk():
    import finquint.orchestration.models as models
    assert "ClaudePlug" not in models.__all__
    source = open(models.__file__.replace("__init__.py", "core.py"), encoding="utf-8").read()
    assert "anthropic" not in source
