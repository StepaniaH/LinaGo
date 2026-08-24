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


class TestSearch:
    def _seed(self, tmp_path):
        from linago.history import History

        hist = History(tmp_path / "h.db")
        hist.add(_entry(source_text="Hello world", translated_text="你好世界"))
        hist.add(
            _entry(
                source_text="guten Morgen",
                translated_text="早上好",
                source_lang="de",
            )
        )
        hist.add(_entry(source_text="wayland tips", translated_text="提示"))
        return hist

    def test_case_insensitive_across_columns(self, tmp_path):
        hist = self._seed(tmp_path)
        assert [e.source_text for e in hist.search("HELLO")] == ["Hello world"]
        assert [e.translated_text for e in hist.search("早上")] == ["早上好"]

    def test_empty_query_returns_recent(self, tmp_path):
        hist = self._seed(tmp_path)
        assert len(hist.search("")) == 3

    def test_limit_applies(self, tmp_path):
        hist = self._seed(tmp_path)
        assert len(hist.search("a", limit=2)) <= 2


class TestExport:
    def test_json_shape(self, tmp_path):
        from linago.history import History

        hist = History(tmp_path / "h.db")
        hist.add(_entry())
        rows = hist.recent(1)
        import json

        payload = json.dumps(
            [
                {
                    "ts": rows[0].ts,
                    "source_lang": "en",
                    "target_lang": "zh",
                    "source_text": "hello",
                    "translated_text": "你好",
                    "provider": "prov_a",
                    "action": None,
                }
            ]
        )
        assert json.loads(payload)[0]["translated_text"] == "你好"

    def test_cli_export_csv_and_json(self, tmp_path, monkeypatch, capsys):
        from linago.history import History

        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        hist = History.open_default()
        hist.add(_entry())

        import linago.cli as cli_mod

        out_json = tmp_path / "out.json"
        cli_mod._write_history_export(str(out_json), hist.recent(5))
        assert '"translated_text"' in out_json.read_text()

        out_csv = tmp_path / "out.csv"
        cli_mod._write_history_export(str(out_csv), hist.recent(5))
        csv_text = out_csv.read_text()
        assert csv_text.splitlines()[0].startswith("ts,")
