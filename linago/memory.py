"""Per-application language memory (opt-in).

Remembers which source language each application tends to carry
(keyed by the Hyprland window class) and lets that history bias the
``auto`` detection for new popups. Records live in a small JSONL file
under the cache directory, trimmed to the most recent entries.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from pathlib import Path

from linago.paths import cache_dir

logger = logging.getLogger(__name__)

_MAX_RECORDS = 500


class LanguageMemory:
    def __init__(self, path: Path, *, enabled: bool = True):
        self._path = path
        self._enabled = enabled
        self._lock = threading.Lock()

    @classmethod
    def open_default(cls, *, enabled: bool) -> LanguageMemory:
        return cls(cache_dir() / "lang-memory.jsonl", enabled=enabled)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def record(self, app_class: str, lang: str) -> None:
        """Append one (class → language) observation."""
        if not self._enabled or not app_class or not lang:
            return
        line = json.dumps({"class": app_class, "lang": lang, "ts": time.time()})
        try:
            with self._lock:
                lines = []
                if self._path.exists():
                    lines = self._path.read_text(encoding="utf-8").splitlines()
                lines.append(line)
                if len(lines) > _MAX_RECORDS:
                    lines = lines[-_MAX_RECORDS:]
                tmp = self._path.with_suffix(".tmp")
                tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
                tmp.replace(self._path)
        except OSError:
            logger.warning("failed to record language memory", exc_info=True)

    def vote(self, app_class: str) -> str | None:
        """Majority language observed for this class; None on ties."""
        if not self._enabled or not app_class or not self._path.exists():
            return None
        counts: Counter[str] = Counter()
        try:
            with self._lock:
                lines = self._path.read_text(encoding="utf-8").splitlines()
            for raw in lines:
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if rec.get("class") == app_class:
                    lang = rec.get("lang")
                    if isinstance(lang, str):
                        counts[lang] += 1
        except OSError:
            logger.warning("failed to read language memory", exc_info=True)
            return None
        if not counts:
            return None
        top_two = counts.most_common(2)
        if len(top_two) == 2 and top_two[0][1] == top_two[1][1]:
            return None  # ambiguous: let the script heuristic decide
        return top_two[0][0]
