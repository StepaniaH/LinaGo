"""Tests for the local translation history store."""

from __future__ import annotations

import time
from pathlib import Path

from linago.history import Entry, History


def _entry(
    *,
    ts: float | None = None,
    source_lang: str = "en",
    target_lang: str = "zh",
    source_text: str = "hello",
    translated_text: str = "你好",
    provider: str | None = "prov_a",
    action: str | None = None,
) -> Entry:
    return Entry(
        ts=ts if ts is not None else time.time(),
        source_lang=source_lang,
        target_lang=target_lang,
        source_text=source_text,
        translated_text=translated_text,
        provider=provider,
        action=action,
    )


def test_add_and_recent_roundtrip(tmp_path: Path):
    hist = History(tmp_path / "history.db")
    hist.add(_entry(source_text="one", translated_text="一"))
    hist.add(
        _entry(
            source_text="two",
            translated_text="二",
            source_lang="ja",
            target_lang="en",
            provider="p2",
            action="explain",
        )
    )
    rows = hist.recent()
    assert len(rows) == 2
    assert rows[0].source_text == "two"  # newest first
    assert rows[0].source_lang == "ja"
    assert rows[0].action == "explain"
    assert rows[1].translated_text == "一"


def test_recent_limit(tmp_path: Path):
    hist = History(tmp_path / "history.db")
    for i in range(5):
        hist.add(_entry(ts=i, source_text=f"t{i}"))
    assert len(hist.recent(limit=3)) == 3


def test_clear_removes_everything(tmp_path: Path):
    hist = History(tmp_path / "history.db")
    hist.add(_entry())
    hist.add(_entry())
    removed = hist.clear()
    assert removed == 2
    assert hist.recent() == []


def test_database_persists_across_instances(tmp_path: Path):
    History(tmp_path / "h.db").add(_entry(source_text="persist"))
    rows = History(tmp_path / "h.db").recent()
    assert len(rows) == 1
    assert rows[0].source_text == "persist"
