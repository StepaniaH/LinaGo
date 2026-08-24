"""Local translation history backed by SQLite.

The database lives under the cache directory; every completed popup
translation is recorded there so `linago --history` can replay it.
Storage failures are logged and swallowed by callers: losing a diary
entry must never break the translation itself.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from linago.paths import cache_dir

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS translations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    source_lang TEXT NOT NULL,
    target_lang TEXT NOT NULL,
    source_text TEXT NOT NULL,
    translated_text TEXT NOT NULL,
    provider TEXT,
    action TEXT
);
CREATE INDEX IF NOT EXISTS idx_translations_ts ON translations(ts DESC);
"""


@dataclass(frozen=True)
class Entry:
    ts: float
    source_lang: str
    target_lang: str
    source_text: str
    translated_text: str
    provider: str | None = None
    action: str | None = None


class History:
    def __init__(self, db_path: Path):
        self._path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @classmethod
    def open_default(cls) -> History:
        return cls(cache_dir() / "history.db")

    def add(self, entry: Entry) -> None:
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO translations (ts, source_lang, target_lang,"
                    " source_text, translated_text, provider, action)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry.ts or time.time(),
                        entry.source_lang,
                        entry.target_lang,
                        entry.source_text,
                        entry.translated_text,
                        entry.provider,
                        entry.action,
                    ),
                )
        except sqlite3.Error:
            logger.warning("failed to record history entry", exc_info=True)

    def recent(self, limit: int = 20) -> list[Entry]:
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT ts, source_lang, target_lang, source_text,"
                    " translated_text, provider, action FROM translations"
                    " ORDER BY ts DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        except sqlite3.Error:
            logger.warning("failed to read history", exc_info=True)
            return []
        return [
            Entry(
                ts=r[0],
                source_lang=r[1],
                target_lang=r[2],
                source_text=r[3],
                translated_text=r[4],
                provider=r[5],
                action=r[6],
            )
            for r in rows
        ]

    def search(self, query: str, limit: int = 20) -> list[Entry]:
        """Case-insensitive containment match over both text columns.

        Filtering happens in Python rather than SQL ``LIKE`` so CJK
        queries behave the same as Latin ones; the store is small
        enough for a linear scan.
        """
        needle = query.strip().casefold()
        if not needle:
            return self.recent(limit)
        matches = [
            entry
            for entry in self.recent(1000)
            if needle in entry.source_text.casefold()
            or needle in entry.translated_text.casefold()
        ]
        return matches[:limit]

    def clear(self) -> int:
        try:
            with self._lock, self._conn:
                cur = self._conn.execute("DELETE FROM translations")
                return cur.rowcount
        except sqlite3.Error:
            logger.warning("failed to clear history", exc_info=True)
            return 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()
