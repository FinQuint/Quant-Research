"""Opt-in rebuild behavior (ContextPolicy.prioritize_uncited, escalate_identical_rebuild), found by
the real-Claude context sweep: at utilization 0.8 the backtest task's criterion C6 lost its
evidence to the budget, Claude left C6 uncited instead of inventing a citation, and each rebuild
(which fills to at most budget x threshold) sent the same request again before ESCALATE."""
from dataclasses import replace
import json

import pytest

from finquint.orchestration.context import ContextBuilder, ContextPolicy, ContextSession, InMemoryEvidenceStore
from finquint.orchestration.context.tags import parse_line, parse_ref
from finquint.orchestration.evals import context as cx
from finquint.orchestration.models import Capabilities, ModelCaller, ModelReply, TokenCounter, Usage
from finquint.orchestration.models.socket import SOCKET_VERSION


class EvidenceBoundPlug:
    """Deterministic stand-in for the behavior Claude showed: cite a criterion's evidence only when
    an evidence line on its topic is in the request; otherwise leave it uncited. No exact counter,
    so requests are budgeted with the chars/4 estimate like the Claude plug's."""
    socket_version = SOCKET_VERSION

    def __init__(self, window=cx.PLUG["context_window"], bad_first=0):
        self.name, self.capabilities = "evidence-bound", Capabilities(context_window=window)
        self.requests, self.bad_first = [], bad_first

    def complete(self, request):
        self.requests.append(request)
        topics, evidence = {}, {}
        for line in request.text().splitlines():
            tag = parse_line(line)
            if tag is None:
                continue
            if tag.kind == "criterion":
                topics[tag.positional[0]] = tag.fields["topic"]
            elif tag.kind == "evidence":
                evidence.setdefault(tag.fields["topic"], parse_ref(tag))
        criteria = []
        for cid, topic in topics.items():
            ref = evidence.get(topic)
            cites = [{"evidence_id": ref[0], "version": ref[1]}] if ref else []
            if len(self.requests) <= self.bad_first:
                cites = [{"evidence_id": "ghost", "version": "1"}]
            criteria.append({"id": cid, "citations": cites})
        report = {"run_id": request.run_id, "code_sha": "golden", "criteria": criteria}
        return ModelReply(json.dumps(report), self.name, Usage(1, 1))


def ask(task, policy, model):
    store = InMemoryEvidenceStore()
    builder = ContextBuilder(policy, counter=TokenCounter(model), window_tokens=model.capabilities.context_window)
    session = ContextSession(task.card, builder=builder, retriever=policy.retriever(store),
                             caller=ModelCaller(model), store=store, system=cx.SYSTEM)
    for step in task.steps:
        if step.kind == "checkpoint":
            session.checkpoint()
            continue
        before = len(session.history)
        cx._apply(step, store, session.history, [])
        for entry in session.history[before:]:
            if entry.kind == "tool_call":
                session.loops.observe(entry.fingerprint, entry.state_hash)
    return session.ask(cx.INSTRUCTION)


BACKTEST = cx.TASKS[1]
AT_08 = replace(ContextPolicy(), utilization_threshold=0.8, history_last_n=16)


def test_defaults_are_off_and_validated():
    policy = ContextPolicy()
    assert policy.prioritize_uncited is False and policy.escalate_identical_rebuild is False
    assert ContextPolicy.load().prioritize_uncited is False      # configs/context_policy.json unchanged
    with pytest.raises(ValueError):
        ContextPolicy(prioritize_uncited=1)


def test_reproduces_the_claude_failure_with_both_switches_off():
    model = EvidenceBoundPlug()
    result = ask(BACKTEST, AT_08, model)
    assert result.status == "escalated" and "C6: no citation" in result.reason
    assert len(model.requests) == 3
    assert len({r.digest() for r in model.requests[1:]}) == 1      # the rebuilds repeat one request


def test_priority_puts_the_uncited_criterions_evidence_first_and_recovers():
    model = EvidenceBoundPlug()
    result = ask(BACKTEST, replace(AT_08, prioritize_uncited=True), model)
    assert result.status == "ok" and result.rebuilds >= 1
    cited = {c["id"]: c["citations"] for c in result.output["criteria"]}
    assert cited["C6"] and cited["C6"][0]["evidence_id"] == "wk-oos_window"
    assert "wk-oos_window" in model.requests[-1].text() and "wk-oos_window" not in model.requests[0].text()


def test_guard_skips_a_hopeless_identical_retry():
    off, on = EvidenceBoundPlug(), EvidenceBoundPlug()
    ask(BACKTEST, AT_08, off)
    result = ask(BACKTEST, replace(AT_08, escalate_identical_rebuild=True), on)
    assert result.status == "escalated" and "no evidence for oos_window" in result.reason
    assert len(on.requests) == 1 and len(off.requests) == 3     # the rebuild repeats the first request


def test_guard_still_retries_when_the_evidence_is_in_the_request():
    """A bad output with the evidence present may be a sampled mistake: the retry runs."""
    model = EvidenceBoundPlug(bad_first=1)
    result = ask(cx.TASKS[0], replace(ContextPolicy(), utilization_threshold=0.9, history_last_n=16,
                                      escalate_identical_rebuild=True), model)
    assert result.status == "ok" and len(model.requests) == 2


def test_both_switches_together_recover_instead_of_escalating():
    model = EvidenceBoundPlug()
    policy = replace(AT_08, prioritize_uncited=True, escalate_identical_rebuild=True)
    assert ask(BACKTEST, policy, model).status == "ok"


def test_simulated_sweep_with_both_switches_keeps_quality_and_never_adds_escalations():
    from finquint.orchestration.demos import claude_sweep as sweep
    off = sweep.run_all(cx.plug())
    on = sweep.run_all(cx.plug(), {"prioritize_uncited": True, "escalate_identical_rebuild": True})
    fails = lambda r: r["forgotten_criteria"] + r["citation_failures"] + r["wrong_status"]
    assert sum(map(fails, on["sweeps"]["context"])) <= sum(map(fails, off["sweeps"]["context"]))
    assert on["sweeps"]["rebuilds"] == off["sweeps"]["rebuilds"] or all(
        a["false_escalations"] <= b["false_escalations"] and a["missed_escalations"] <= b["missed_escalations"]
        for a, b in zip(on["sweeps"]["rebuilds"], off["sweeps"]["rebuilds"]))
    assert sum(r["model_calls"] for r in on["sweeps"]["context"]) < sum(r["model_calls"] for r in off["sweeps"]["context"])
    assert on["totals"]["builder"] == off["totals"]["builder"]
