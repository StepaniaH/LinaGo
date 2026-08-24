"""Tests for multi-provider comparison helpers."""

from __future__ import annotations

from linago.compare import pane_maxima, resolve_providers
from linago.config import Provider


def _config(*names: str) -> object:
    providers = {
        n: Provider(name=n, type="ollama", label=n, base_url="u", model="m")
        for n in names
    }
    from linago.config import AppConfig

    return AppConfig(active=names[0], providers=providers)


class TestResolveProviders:
    def test_order_preserved_and_deduped(self):
        cfg = _config("a", "b")
        got = resolve_providers(["b", "a", "b"], cfg)
        assert [p.name for p in got] == ["b", "a"]

    def test_unknown_names_skipped(self):
        cfg = _config("a")
        got = resolve_providers(["ghost", "a"], cfg)
        assert [p.name for p in got] == ["a"]

    def test_capped_at_four(self):
        cfg = _config("a", "b", "c", "d", "e")
        got = resolve_providers(["e", "d", "c", "b", "a"], cfg)
        assert [p.name for p in got] == ["e", "d", "c", "b"]


class TestPaneMaxima:
    def test_even_split(self):
        assert pane_maxima(300, 3) == [100, 100, 100]

    def test_floor_keeps_panes_usable(self):
        assert pane_maxima(90, 4, min_h=60) == [60, 60, 60, 60]

    def test_zero_count(self):
        assert pane_maxima(500, 0) == []
