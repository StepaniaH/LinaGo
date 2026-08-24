"""Tests for streaming backends using stubbed HTTP responses."""

from __future__ import annotations

import json
import threading

import pytest

import linago.backends as backends
from linago.backends import (
    _emit_coalesced,
    _stream_ollama,
    _stream_openai,
    stream_completion,
)
from linago.config import Provider


class StubResponse:
    def __init__(self, lines: list[str]):
        self._lines = lines

    def raise_for_status(self):
        pass

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)


@pytest.fixture
def provider():
    return Provider(
        name="p",
        type="openai",
        label="P",
        base_url="https://p.test/v1",
        model="m",
        api_key="k-test",
    )


def _sse(*deltas: dict) -> list[str]:
    lines = ["data: " + json.dumps({"choices": [d]}) for d in deltas]
    lines.append("data: [DONE]")
    return lines


class TestOpenAIStream:
    def test_deltas_concatenate_until_done(self, monkeypatch, provider):
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["payload"] = kwargs["json"]
            return StubResponse(
                [
                    ": keepalive",  # SSE comment: ignored
                    "",  # blank line: ignored
                    "data: not-json",  # malformed: ignored
                    *_sse(
                        {"delta": {"content": "你"}},
                        {"delta": {"content": "好"}},
                    ),
                ]
            )

        monkeypatch.setattr(backends.requests, "post", fake_post)
        tokens: list[str] = []
        _stream_openai(provider, "prompt", tokens.append, threading.Event(), 30)
        assert captured["url"] == "https://p.test/v1/chat/completions"
        assert captured["payload"]["model"] == "m"
        assert tokens[-1] == "你好"

    def test_message_content_fallback(self, monkeypatch, provider):
        chunk = json.dumps(
            {
                "choices": [
                    {
                        "delta": {},
                        "message": {"content": "final"},
                        "finish_reason": "stop",
                    }
                ]
            }
        )
        monkeypatch.setattr(
            backends.requests,
            "post",
            lambda url, **kw: StubResponse(["data: " + chunk]),
        )
        tokens: list[str] = []
        _stream_openai(provider, "p", tokens.append, threading.Event(), 30)
        assert any(t == "final" for t in tokens)

    def test_cancel_stops_before_processing(self, monkeypatch, provider):
        calls: list[str] = []

        def fake_post(url, **kwargs):
            calls.append(url)
            return StubResponse(_sse({"delta": {"content": "x"}}))

        monkeypatch.setattr(backends.requests, "post", fake_post)
        tokens: list[str] = []
        cancel = threading.Event()
        cancel.set()
        _stream_openai(provider, "p", tokens.append, cancel, 30)
        assert calls  # request went out
        assert tokens == []  # but nothing was emitted


class TestOllamaStream:
    def test_accumulates_until_done(self, monkeypatch):
        p = Provider(
            name="o",
            type="ollama",
            label="O",
            base_url="http://127.0.0.1:11434",
            model="m",
        )
        lines = [
            json.dumps({"response": "你"}),
            json.dumps({"response": "好", "done": True}),
            json.dumps({"response": "NEVER"}),
        ]

        def fake_post(url, **kwargs):
            assert url == "http://127.0.0.1:11434/api/generate"
            return StubResponse(lines)

        monkeypatch.setattr(backends.requests, "post", fake_post)
        tokens: list[str] = []
        _stream_ollama(p, "prompt", tokens.append, threading.Event(), 30)
        assert tokens[-1] == "你好"


class TestCoalescer:
    def test_first_token_emits_immediately(self, monkeypatch):
        clock = iter([100.0])
        monkeypatch.setattr(backends.time, "monotonic", lambda: next(clock))
        seen: list[str] = []
        _emit_coalesced(seen.append, "a", {"last": 0.0})
        assert seen == ["a"]

    def test_throttles_within_interval(self, monkeypatch):
        times = iter([10.00, 10.01, 10.05])
        monkeypatch.setattr(backends.time, "monotonic", lambda: next(times))
        seen: list[str] = []
        state = {"last": 0.0}
        _emit_coalesced(seen.append, "a", state)
        _emit_coalesced(seen.append, "ab", state)  # throttled
        _emit_coalesced(seen.append, "abc", state)  # 50ms later
        assert seen == ["a", "abc"]

    def test_force_bypasses_throttle(self, monkeypatch):
        monkeypatch.setattr(backends.time, "monotonic", lambda: 5.0)
        seen: list[str] = []
        state = {"last": 5.0}
        _emit_coalesced(seen.append, "done", state, force=True)
        assert seen == ["done"]


class TestDispatch:
    def test_unsupported_type_raises(self):
        p = Provider(name="x", type="alien", label="X", base_url="u", model="m")
        with pytest.raises(RuntimeError, match="unsupported"):
            stream_completion(p, "p", lambda t: None, threading.Event())

    def test_openai_requires_key(self):
        p = Provider(name="x", type="openai", label="X", base_url="u", model="m")
        with pytest.raises(RuntimeError, match="API key"):
            stream_completion(p, "p", lambda t: None, threading.Event())
