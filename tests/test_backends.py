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
    def __init__(self, lines: list[str] | None = None, json_data=None):
        self._lines = lines or []
        self._json = json_data

    def raise_for_status(self):
        pass

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)

    def json(self):
        if self._json is None:
            raise ValueError("no JSON body in stub")
        return self._json


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

    def test_missing_key_still_attempts_request(self, monkeypatch):
        """Self-hosted gateways often need no key; the request goes out
        without an Authorization header and the provider answers."""
        captured = {}

        def fake_post(url, **kw):
            captured["headers"] = kw.get("headers")
            return StubResponse(_sse({"delta": {"content": "x"}}))

        monkeypatch.setattr(backends.requests, "post", fake_post)
        p = Provider(name="x", type="openai", label="X", base_url="u", model="m")
        tokens: list[str] = []
        stream_completion(p, "p", tokens.append, threading.Event())
        assert "Authorization" not in (captured["headers"] or {})
        assert tokens[-1] == "x"


class TestVisionOCR:
    @staticmethod
    def _provider(type_: str = "openai") -> Provider:
        return Provider(
            name="v",
            type=type_,
            label="V",
            base_url="https://v.test",
            model="vl-model",
            api_key="k-test",
        )

    def test_openai_payload_and_result(self, monkeypatch, tmp_path):
        import base64 as b64mod

        png = tmp_path / "shot.png"
        png.write_bytes(b"PNGDATA")
        captured: dict = {}

        def fake_post(url, **kw):
            captured["url"] = url
            captured["payload"] = kw["json"]
            return StubResponse(_sse({"delta": {"content": "  hi there  "}}))

        monkeypatch.setattr(backends.requests, "post", fake_post)
        assert backends.vision_ocr(TestVisionOCR._provider(), png) == "hi there"

        content = captured["payload"]["messages"][0]["content"]
        assert content[0] == {"type": "text", "text": backends.TRANSCRIBE_PROMPT}
        data_url = content[1]["image_url"]["url"]
        prefix, _, encoded = data_url.partition(",")
        assert prefix == "data:image/png;base64"
        assert b64mod.b64decode(encoded) == b"PNGDATA"
        assert captured["payload"]["model"] == "vl-model"

    def test_ollama_sends_images_field(self, monkeypatch, tmp_path):
        import base64 as b64mod

        png = tmp_path / "shot.png"
        png.write_bytes(b"IMG")
        captured: dict = {}

        def fake_post(url, **kw):
            captured["url"] = url
            captured["payload"] = kw["json"]
            return StubResponse([json.dumps({"response": "recognized", "done": True})])

        monkeypatch.setattr(backends.requests, "post", fake_post)
        result = backends.vision_ocr(TestVisionOCR._provider("ollama"), png)
        assert result == "recognized"
        assert captured["url"].endswith("/api/generate")
        assert captured["payload"]["images"] == [b64mod.b64encode(b"IMG").decode()]
        assert captured["payload"]["stream"] is True

    def test_empty_model_output_is_empty_string(self, monkeypatch, tmp_path):
        png = tmp_path / "shot.png"
        png.write_bytes(b"IMG")
        monkeypatch.setattr(
            backends.requests,
            "post",
            lambda url, **kw: StubResponse(json_data={"response": "   "}),
        )
        assert backends.vision_ocr(TestVisionOCR._provider("ollama"), png) == ""

    def test_transport_failure_is_none(self, monkeypatch, tmp_path):
        png = tmp_path / "shot.png"
        png.write_bytes(b"IMG")

        def broken(url, **kw):
            raise backends.requests.ConnectionError("refused")

        monkeypatch.setattr(backends.requests, "post", broken)
        assert backends.vision_ocr(TestVisionOCR._provider(), png) is None

    def test_missing_key_sends_no_auth_header(self, monkeypatch, tmp_path):
        png = tmp_path / "shot.png"
        png.write_bytes(b"IMG")
        captured: dict = {}

        def fake_post(url, **kw):
            captured["headers"] = kw["headers"]
            return StubResponse(_sse({"delta": {"content": "text"}}))

        monkeypatch.setattr(backends.requests, "post", fake_post)
        provider = Provider(
            name="v",
            type="openai",
            label="V",
            base_url="u",
            model="m",
        )
        assert backends.vision_ocr(provider, png) == "text"
        assert "Authorization" not in captured["headers"]


class TestRetry:
    def _ok(self):
        return StubResponse(json_data={})

    def test_retries_connection_errors_then_succeeds(self, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr(backends.time, "sleep", sleeps.append)
        attempts = {"n": 0}

        def flaky(url, **kw):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise backends.requests.ConnectionError("reset")
            return self._ok()

        monkeypatch.setattr(backends.requests, "post", flaky)
        resp = backends._post_with_retry("https://x.test", json={})
        assert resp is not None
        assert attempts["n"] == 3
        assert sleeps == [0.5, 1.0]  # exponential backoff

    def test_gives_up_after_attempts(self, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr(backends.time, "sleep", sleeps.append)

        def always_down(url, **kw):
            raise backends.requests.Timeout("too slow")

        monkeypatch.setattr(backends.requests, "post", always_down)
        with pytest.raises(backends.requests.Timeout):
            backends._post_with_retry("https://x.test")
        assert len(sleeps) == 2  # retried twice before giving up

    def test_non_transport_errors_are_not_retried(self, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr(backends.time, "sleep", sleeps.append)

        def rejected(url, **kw):
            raise backends.requests.HTTPError("401 unauthorized")

        monkeypatch.setattr(backends.requests, "post", rejected)
        with pytest.raises(backends.requests.HTTPError):
            backends._post_with_retry("https://x.test")
        assert sleeps == []


class TestProviderOptions:
    def _provider(self, type_: str = "openai", **opts) -> Provider:
        return Provider(
            name="p",
            type=type_,
            label="P",
            base_url="https://p.test",
            model="m",
            api_key="k-test" if type_ == "openai" else None,
            **opts,
        )

    def test_openai_temperature_default_and_override(self, monkeypatch):
        captured: dict = {}

        def fake_post(url, **kw):
            captured["payload"] = kw["json"]
            return StubResponse(_sse({"delta": {"content": "x"}}))

        monkeypatch.setattr(backends.requests, "post", fake_post)

        cancel = threading.Event()
        _stream_openai(self._provider(), "p", lambda t: None, cancel, 30)
        assert captured["payload"]["temperature"] == 0.2
        assert "max_tokens" not in captured["payload"]

        _stream_openai(
            self._provider(temperature=0.7, max_tokens=256),
            "p",
            lambda t: None,
            threading.Event(),
            30,
        )
        assert captured["payload"]["temperature"] == 0.7
        assert captured["payload"]["max_tokens"] == 256

    def test_ollama_options_mapping(self, monkeypatch):
        captured: dict = {}

        def fake_post(url, **kw):
            captured["url"] = url
            captured["body"] = kw["json"]
            return StubResponse([json.dumps({"response": "y", "done": True})])

        monkeypatch.setattr(backends.requests, "post", fake_post)
        p = self._provider("ollama", temperature=0.3, max_tokens=64)
        _stream_ollama(p, "p", lambda t: None, threading.Event(), 30)
        assert captured["body"]["options"] == {
            "temperature": 0.3,
            "num_predict": 64,
        }

    def test_provider_timeout_wins_over_default(self, monkeypatch):
        seen: dict = {}

        def fake_retry(url, **kw):
            seen["timeout"] = kw["timeout"]
            return StubResponse(_sse({"delta": {"content": "x"}}))

        monkeypatch.setattr(backends, "_post_with_retry", fake_retry)
        stream_completion(
            self._provider(timeout_s=7), "p", lambda t: None, threading.Event()
        )
        assert seen["timeout"] == 7

        seen.clear()
        stream_completion(
            self._provider(),
            "p",
            lambda t: None,
            threading.Event(),
        )
        assert seen["timeout"] == 120


class TestStreamVisionOCR:
    _provider = staticmethod(
        TestVisionOCR._provider.__func__
        if hasattr(TestVisionOCR._provider, "__func__")
        else TestVisionOCR._provider
    )

    def test_progress_receives_accumulated_text(self, monkeypatch, tmp_path):
        png = tmp_path / "shot.png"
        png.write_bytes(b"IMG")
        monkeypatch.setattr(
            backends.requests,
            "post",
            lambda url, **kw: StubResponse(
                [
                    json.dumps({"response": "你"}),
                    "",
                    json.dumps({"response": "好", "done": True}),
                ]
            ),
        )
        seen: list[str] = []
        result = backends.stream_vision_ocr(
            self._provider("ollama"), png, seen.append, None
        )
        assert result == "你好"
        assert seen[-1] == "你好"

    def test_sse_deltas_stream(self, monkeypatch, tmp_path):
        png = tmp_path / "shot.png"
        png.write_bytes(b"IMG")
        monkeypatch.setattr(
            backends.requests,
            "post",
            lambda url, **kw: StubResponse(
                _sse({"delta": {"content": "hi"}}) + ["data: not-json"]
            ),
        )
        seen: list[str] = []
        provider = self._provider("openai")
        result = backends.stream_vision_ocr(provider, png, seen.append, None)
        assert result == "hi"
        assert seen[-1] == "hi"

    def test_cancel_returns_partial(self, monkeypatch, tmp_path):
        import threading

        png = tmp_path / "shot.png"
        png.write_bytes(b"IMG")
        cancel = threading.Event()
        cancel.set()
        monkeypatch.setattr(
            backends.requests,
            "post",
            lambda url, **kw: StubResponse(
                [json.dumps({"response": "partial", "done": False})]
            ),
        )
        provider = self._provider("ollama")
        result = backends.stream_vision_ocr(provider, png, None, cancel)
        assert result == ""

    def test_blocking_wrapper_matches_stream_result(self, monkeypatch, tmp_path):
        png = tmp_path / "shot.png"
        png.write_bytes(b"IMG")
        monkeypatch.setattr(
            backends.requests,
            "post",
            lambda url, **kw: StubResponse(
                [json.dumps({"response": "final", "done": True})]
            ),
        )
        assert backends.vision_ocr(self._provider("ollama"), png) == "final"
