from types import SimpleNamespace

import anthropic
import httpx
import pytest
from pydantic import BaseModel

from text2sql.llm.client import LLMClient, LLMOutputError
from text2sql.observability.tracing import MemoryTracer


class Answer(BaseModel):
    """A test answer."""

    sql: str
    confidence: float


def _resp(payload, name="respond", inp=10, out=5):
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", name=name, input=payload)],
        usage=SimpleNamespace(input_tokens=inp, output_tokens=out),
    )


class FakeSDK:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls += 1
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o


def _client(settings, outcomes):
    tracer = MemoryTracer()
    sdk = FakeSDK(outcomes)
    return LLMClient(settings, sdk_client=sdk, tracer=tracer), sdk, tracer


def test_success_traces_tokens(settings):
    c, sdk, tr = _client(settings, [_resp({"sql": "SELECT 1", "confidence": 0.9})])
    r = c.complete_structured(agent_name="gen", system="s", user="u", response_model=Answer)
    assert r.parsed.sql == "SELECT 1" and r.attempts == 1
    assert (r.usage.input_tokens, r.usage.output_tokens) == (10, 5)
    assert len(tr.calls) == 1 and tr.calls[0].success and tr.calls[0].agent_name == "gen"


def test_reasks_after_invalid_output(settings):
    c, sdk, tr = _client(
        settings,
        [_resp({"sql": "SELECT 1"}), _resp({"sql": "SELECT 1", "confidence": 0.5})],
    )
    r = c.complete_structured(agent_name="gen", system="s", user="u", response_model=Answer)
    assert r.attempts == 2 and sdk.calls == 2
    assert [x.success for x in tr.calls] == [False, True]


def test_gives_up_after_max_output_attempts(settings):
    c, sdk, _ = _client(settings, [_resp({"bad": 1}), _resp({"bad": 2})])
    with pytest.raises(LLMOutputError):
        c.complete_structured(agent_name="gen", system="s", user="u", response_model=Answer)
    assert sdk.calls == 2  # bounded: never loops forever


def test_retries_transient_errors(settings):
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))
    c, sdk, _ = _client(settings, [err, err, _resp({"sql": "SELECT 1", "confidence": 1})])
    r = c.complete_structured(agent_name="gen", system="s", user="u", response_model=Answer)
    assert r.parsed.confidence == 1 and sdk.calls == 3


def test_transient_errors_are_bounded(settings):
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))
    c, sdk, tr = _client(settings, [err, err, err, err])
    with pytest.raises(anthropic.APIConnectionError):
        c.complete_structured(agent_name="gen", system="s", user="u", response_model=Answer)
    assert sdk.calls == 3 and tr.calls[-1].success is False
