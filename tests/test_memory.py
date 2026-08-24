"""Tests for per-application language memory."""

from __future__ import annotations

import linago.placement as placement
from linago.lang import resolve_pair
from linago.memory import LanguageMemory
from linago.placement import get_active_window_class


def _store(tmp_path, **kwargs) -> LanguageMemory:
    return LanguageMemory(tmp_path / "mem.jsonl", **kwargs)


class TestRecordAndVote:
    def test_majority_wins(self, tmp_path):
        mem = _store(tmp_path)
        for _ in range(3):
            mem.record("firefox", "en")
        mem.record("firefox", "zh")
        assert mem.vote("firefox") == "en"

    def test_tie_returns_none(self, tmp_path):
        mem = _store(tmp_path)
        mem.record("code", "zh")
        mem.record("code", "en")
        assert mem.vote("code") is None

    def test_unknown_class_is_none(self, tmp_path):
        assert _store(tmp_path).vote("never-seen") is None

    def test_disabled_store_neither_records_nor_votes(self, tmp_path):
        mem = _store(tmp_path, enabled=False)
        mem.record("app", "ja")
        assert not (tmp_path / "mem.jsonl").exists()
        assert mem.vote("app") is None

    def test_records_capped(self, tmp_path):
        mem = _store(tmp_path)
        for i in range(600):
            mem.record(f"app{i}", "en")
        lines = (tmp_path / "mem.jsonl").read_text().splitlines()
        assert len(lines) == 500

    def test_corrupt_lines_are_skipped(self, tmp_path):
        path = tmp_path / "mem.jsonl"
        path.write_text('{"class": "a", "lang": "ru"}\nnot-json\n')
        assert LanguageMemory(path).vote("a") == "ru"


def test_active_window_class_parsing(monkeypatch):
    monkeypatch.setattr(
        placement,
        "_run_hyprctl",
        lambda args: '{"class": "org.wezfurlong.wezterm"}',
    )
    assert get_active_window_class() == "org.wezfurlong.wezterm"


def test_resolve_pair_detected_override():
    # Memory bias: latin text remembered as zh source for this app.
    pair = resolve_pair("hello world", "auto", "auto", detected="zh")
    assert pair.source == "zh"
    assert pair.target == "en"
    # Unknown hints fall back to the heuristic.
    fallback = resolve_pair("hello", "auto", "auto", detected="xx")
    assert fallback.source == "en"
