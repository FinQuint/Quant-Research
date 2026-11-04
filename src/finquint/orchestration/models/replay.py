"""Record and replay model replies, so a run with a real model becomes a repeatable test.

RecordingPlug wraps any plug and keeps each reply under the request's digest. ReplayPlug
serves those replies back and never calls a model; a request it has no reply for raises
ReplayMiss (a refusal, so the core escalates instead of trying another model).
"""
from dataclasses import replace
import json
from pathlib import Path

from .socket import SOCKET_VERSION, Capabilities, ModelRefused, ModelReply, Usage

RECORDING_FORMAT = 1


class ReplayMiss(ModelRefused):
    """The replay has no recorded reply for this request."""


def _reply_dict(reply):
    return {"text": reply.text, "model": reply.model,
            "usage": {"input_tokens": reply.usage.input_tokens, "output_tokens": reply.usage.output_tokens,
                      "exact": reply.usage.exact}}


def _reply(data):
    return ModelReply(data["text"], data["model"], Usage(**data["usage"]))


class RecordingPlug:
    socket_version = SOCKET_VERSION

    def __init__(self, plug):
        self.plug = plug
        self.name = plug.name
        self.capabilities = plug.capabilities
        self.recordings = {}

    def count_tokens(self, text):
        return self.plug.count_tokens(text)

    def complete(self, request):
        reply = self.plug.complete(request)
        self.recordings[request.digest()] = _reply_dict(reply)
        return reply

    def document(self):
        return {"format": RECORDING_FORMAT, "plug": self.name,
                "capabilities": {"context_window": self.capabilities.context_window},
                "replies": dict(sorted(self.recordings.items()))}

    def save(self, path):
        Path(path).write_text(json.dumps(self.document(), indent=1, sort_keys=True) + "\n", encoding="utf-8")


class ReplayPlug:
    socket_version = SOCKET_VERSION

    def __init__(self, name, recordings, *, capabilities, count_tokens=None):
        """count_tokens: the recorded plug's exact counter, if it had one. Requests are built from
        token counts, so a replay must count the same way or its request digests will not match."""
        self.name = name
        self._count = count_tokens
        if capabilities.exact_token_count and count_tokens is None:
            capabilities = replace(capabilities, exact_token_count=False)
        self.capabilities = capabilities
        self.recordings = dict(recordings)
        self.served = []

    @classmethod
    def from_document(cls, data, *, count_tokens=None):
        if data.get("format") != RECORDING_FORMAT:
            raise ValueError(f"unsupported recording format: {data.get('format')}")
        return cls(f"replay:{data['plug']}", data["replies"],
                   capabilities=Capabilities(context_window=data["capabilities"]["context_window"],
                                             exact_token_count=count_tokens is not None),
                   count_tokens=count_tokens)

    @classmethod
    def load(cls, path, *, count_tokens=None):
        return cls.from_document(json.loads(Path(path).read_text(encoding="utf-8")), count_tokens=count_tokens)

    def count_tokens(self, text):
        if self._count is None:
            raise NotImplementedError("this replay has no exact token counter")
        return self._count(text)

    def complete(self, request):
        key = request.digest()
        if key not in self.recordings:
            raise ReplayMiss(f"no recorded reply for request {key[:12]}")
        self.served.append(key)
        return _reply(self.recordings[key])
