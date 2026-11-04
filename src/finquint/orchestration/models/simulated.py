"""SimulatedModel: a deterministic stand-in for a language model, used as the CI gate.

It answers a task report (context.tags.REPORT_SCHEMA) using only the text of the request it
receives, and it makes the two context mistakes the Phase 5 exit test measures:

  * A criterion whose [criterion ...] line it cannot see is forgotten (left out of the report).
  * For each criterion it cites the most recent evidence or mention it can see on that topic.
    If the most recent thing it saw is a stale mention, another run's output or nothing at
    all, the citation is wrong or missing.

Attention (documented simulation assumption, "lost in the middle"): a request longer than
attention_tokens is read as its first half-attention and last half-attention tokens; lines in
between are not seen. A request larger than the context window is refused, as a provider
would refuse it.

It measures the context mechanism, not real model behavior. Real models plug into the same
socket and are scored on the same tasks.
"""
import json

from ..context.tags import parse_line, parse_ref
from .socket import SOCKET_VERSION, Capabilities, ModelRefused, ModelReply, Usage
from .tokens import CharEstimate


class SimulatedModel:
    socket_version = SOCKET_VERSION

    def __init__(self, name="simulated-v1", *, context_window=8192, attention_tokens=None, estimate=None):
        self.name = name
        self.estimate = estimate or CharEstimate()
        self.attention_tokens = attention_tokens or context_window
        if self.attention_tokens < 2:
            raise ValueError("attention_tokens must be at least 2")
        self.capabilities = Capabilities(context_window=context_window, exact_token_count=True)

    def count_tokens(self, text):
        return self.estimate.count(text)

    def visible_lines(self, text):
        lines = text.splitlines()
        if self.count_tokens(text) <= self.attention_tokens:
            return lines
        half = self.attention_tokens // 2
        head, used = [], 0
        for line in lines:
            used += self.count_tokens(line + "\n")
            if used > half:
                break
            head.append(line)
        tail, used = [], 0
        for line in reversed(lines[len(head):]):
            used += self.count_tokens(line + "\n")
            if used > half:
                break
            tail.append(line)
        return head + list(reversed(tail))

    def complete(self, request):
        text = request.text()
        tokens = self.count_tokens(text)
        if tokens + request.max_output_tokens > self.capabilities.context_window:
            raise ModelRefused(f"request of {tokens} tokens exceeds the {self.capabilities.context_window}-token window")
        card, criteria, latest = {}, [], {}
        for line in self.visible_lines(text):
            tag = parse_line(line)
            if tag is None:
                continue
            if tag.kind == "card":
                card = tag.fields
            elif tag.kind == "criterion" and tag.positional:
                if tag.positional[0] not in [c for c, _ in criteria]:
                    criteria.append((tag.positional[0], tag.fields.get("topic")))
            elif tag.kind in ("evidence", "mention") and len(tag.positional) >= 2:
                latest[tag.fields.get("topic")] = parse_ref(tag)   # later lines win
        report = {"run_id": card.get("run", ""), "code_sha": card.get("code_sha", ""), "criteria": [
            {"id": cid, "citations": ([{"evidence_id": latest[topic][0], "version": latest[topic][1]}]
                                      if topic in latest else [])}
            for cid, topic in criteria]}
        body = json.dumps(report, sort_keys=True)
        return ModelReply(body, self.name, Usage(tokens, self.count_tokens(body), exact=True))
