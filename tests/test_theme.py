"""Tests for the theme template pipeline."""

from __future__ import annotations

from pathlib import Path

import pytest

import linago.theme as theme

CSS = Path(__file__).resolve().parent.parent / "config" / "style.css"


def test_dark_render_matches_original_stylesheet():
    settings = {"appearance": {"preset": "dark"}}
    assert theme.render_settings(settings) == CSS.read_text()


def test_unknown_preset_falls_back_to_dark():
    fallback = theme.render_settings({"appearance": {"preset": "neon"}})
    assert fallback == theme.render_settings({"appearance": {}})


def test_font_scale_multiplies_sizes():
    text = theme.render_settings({"appearance": {"preset": "dark", "font_scale": 1.2}})
    assert "font-size: 17px" in text  # 14 * 1.2 -> 17
    assert "font-size: 20px" in text  # 17 * 1.2 -> 20
    assert "border-radius: $radius_card" not in text


def test_scale_is_clamped():
    tiny = theme.resolve_params({"appearance": {"font_scale": 9}})
    assert tiny["title_px"] == f"{round(14 * 1.6)}px"
    huge = theme.resolve_params({"appearance": {"font_scale": 0.1}})
    assert huge["title_px"] == f"{round(14 * 0.7)}px"


def test_accent_overrides_section_labels_only():
    params = theme.resolve_params({"appearance": {"accent": "#3366ff"}})
    assert params["muted_fg"] == "#3366ff"
    assert params["text_fg"] == theme.DARK["text_fg"]


def test_bg_alpha_clamped():
    params = theme.resolve_params({"appearance": {"bg_alpha": 5}})
    assert float(params["bg_alpha"]) == 1.0


class TestSaveAppearance:
    def _write_settings(self, tmp_path: Path) -> Path:
        path = tmp_path / "settings.toml"
        path.write_text(
            '# keep me\n[app]\nprovider = "ollama"\n'
            '\n[providers.ollama]\ntype = "ollama"\n'
            'base_url = "http://x"\nmodel = "m"\n'
        )
        return path

    def test_rejects_unknown_keys(self, tmp_path: Path):
        with pytest.raises(ValueError, match="unknown appearance keys"):
            theme.save_appearance(self._write_settings(tmp_path), {"colour": "#fff"})

    def test_persists_and_regenerates_css(self, tmp_path: Path):
        path = self._write_settings(tmp_path)
        resolved = theme.save_appearance(path, {"preset": "paper", "font_scale": 1.1})
        assert resolved["surface"] == theme.PAPER["surface"]
        text = path.read_text()
        assert "# keep me" in text
        assert "[appearance]" in text
        css = (tmp_path / "style.css").read_text()
        assert "rgba(250, 250, 247, 0.97)" in css
