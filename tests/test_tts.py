"""Tests for TTS synthesis and playback command selection."""

from __future__ import annotations

import pytest

import linago.backends as backends
from linago.backends import StubResponse
from linago.config import Provider
from linago.playback import pick_player


class TestTTSSpeech:
    @staticmethod
    def _provider(type_: str = "openai") -> Provider:
        return Provider(
            name="t",
            type=type_,
            label="T",
            base_url="https://t.test/v1",
            model="tts-1",
            api_key="k-test" if type_ == "openai" else None,
        )

    def test_openai_payload_and_audio_bytes(self, monkeypatch):
        captured: dict = {}

        class AudioResponse(StubResponse):
            content = b"AUDIOBYTES"

        def fake_post(url, **kw):
            captured["url"] = url
            captured["payload"] = kw["json"]
            return AudioResponse(json_data={})

        monkeypatch.setattr(backends.requests, "post", fake_post)
        audio = backends.tts_speech(self._provider(), "hello")
        assert audio == b"AUDIOBYTES"
        assert captured["url"] == "https://t.test/v1/audio/speech"
        assert captured["payload"] == {
            "model": "tts-1",
            "voice": "alloy",
            "input": "hello",
        }

    def test_ollama_type_is_rejected_up_front(self):
        provider = Provider(name="o", type="ollama", label="O", base_url="u", model="m")
        with pytest.raises(RuntimeError, match="TTS"):
            backends.tts_speech(provider, "hello")

    def test_missing_key_raises(self):
        provider = Provider(name="x", type="openai", label="X", base_url="u", model="m")
        with pytest.raises(RuntimeError, match="API key"):
            backends.tts_speech(provider, "hi")


class TestPlayerSelection:
    def test_prefers_first_available(self, monkeypatch):
        available = {"paplay": "/usr/bin/paplay", "aplay": "/usr/bin/aplay"}
        assert pick_player(available.get) == "paplay"

    def test_none_when_no_player(self, monkeypatch):
        assert pick_player(lambda name: None) is None
