"""Theme presets rendered into the popup stylesheet.

GTK4 CSS has no custom properties, so themes are generated: a
``string.Template`` sheet with concrete values is written to
``style.css`` beside settings.toml from ``[appearance]``, which is the
single source of truth. The dark preset reproduces the original
hand-written stylesheet exactly.

Regeneration happens through the web console or by calling
:func:`regenerate`; popups pick up the file on their next open.
"""

from __future__ import annotations

import logging
from pathlib import Path
from string import Template

import linago.configstore as configstore
from linago.paths import ensure_config_dir

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = Path(__file__).resolve().parent / "style.css.template"

_BASE_PX = {
    "title_px": 14,
    "small_px": 13,
    "source_px": 15,
    "text_px": 17,
    "foot_px": 12,
    "swap_px": 16,
    "close_px": 20,
}

DARK = {
    "surface": "30, 30, 30",
    "bg_alpha": "0.94",
    "hov": "rgba(255, 255, 255, 0.10)",
    "hov_strong": "rgba(255, 255, 255, 0.20)",
    "dd_bg": "rgba(255, 255, 255, 0.12)",
    "dd_hover": "rgba(255, 255, 255, 0.18)",
    "border": "rgba(255, 255, 255, 0.08)",
    "title_fg": "#888",
    "muted_fg": "#666",
    "swap_fg": "#777",
    "close_hover_fg": "#ccc",
    "swap_hover_fg": "#ddd",
    "src_fg": "#999",
    "edit_fg": "#b5b5b5",
    "caret_fg": "#d0d0d0",
    "edit_focus_fg": "#d8d8d8",
    "btn_hover_fg": "#ffffff",
    "text_fg": "#e0e0e0",
    "foot_fg": "#9a9a9a",
    "meta_fg": "#b0b0b0",
    "dd_fg": "#f0f0f0",
    "dd_arrow_fg": "#c8c8c8",
    "row_fg": "#e8e8e8",
}

_RADII = {"radius_card": "20px", "radius_ctrl": "10px", "radius_btn": "8px"}

MIDNIGHT = {
    **DARK,
    "surface": "16, 20, 34",
    "bg_alpha": "0.95",
    "border": "rgba(120, 150, 220, 0.14)",
    "title_fg": "#7d92c4",
    "muted_fg": "#6d7ba0",
    "text_fg": "#dbe2f5",
    "row_fg": "#cdd6ee",
}

PAPER = {
    "surface": "250, 250, 247",
    "bg_alpha": "0.97",
    "hov": "rgba(0, 0, 0, 0.06)",
    "hov_strong": "rgba(0, 0, 0, 0.12)",
    "dd_bg": "rgba(0, 0, 0, 0.07)",
    "dd_hover": "rgba(0, 0, 0, 0.12)",
    "border": "rgba(0, 0, 0, 0.12)",
    "title_fg": "#8a8a84",
    "muted_fg": "#767670",
    "swap_fg": "#82827c",
    "close_hover_fg": "#4a4a44",
    "swap_hover_fg": "#3f3f3a",
    "src_fg": "#5f5f58",
    "edit_fg": "#3c3c36",
    "caret_fg": "#22221e",
    "edit_focus_fg": "#14140f",
    "btn_hover_fg": "#000000",
    "text_fg": "#26261f",
    "foot_fg": "#7c7c74",
    "meta_fg": "#55554e",
    "dd_fg": "#33332c",
    "dd_arrow_fg": "#6a6a62",
    "row_fg": "#2e2e27",
}

PRESETS = {"dark": DARK, "midnight": MIDNIGHT, "paper": PAPER}

_SCALE_MIN, _SCALE_MAX = 0.7, 1.6

APPEARANCE_KEYS = {"preset", "accent", "bg_alpha", "font_scale"}


def resolve_params(settings: dict) -> dict:
    """Concrete template values for ``[appearance]`` in *settings*."""
    table = settings.get("appearance") or {}
    name = str(table.get("preset", "dark"))
    params = dict(PRESETS.get(name, DARK))
    try:
        scale = float(table.get("font_scale", 1.0))
    except (TypeError, ValueError):
        scale = 1.0
    scale = min(max(scale, _SCALE_MIN), _SCALE_MAX)
    for key, base in _BASE_PX.items():
        params[key] = f"{round(base * scale)}px"
    params.update(_RADII)
    try:
        bg_alpha = float(table.get("bg_alpha", params["bg_alpha"]))
    except (TypeError, ValueError):
        bg_alpha = float(params["bg_alpha"])
    params["bg_alpha"] = str(min(max(bg_alpha, 0.3), 1.0))
    accent = str(table.get("accent", "")).strip()
    if accent:
        # the accent drives the section labels; everything else stays
        # inside its preset palette
        params["muted_fg"] = accent
    return params


def render_params(params: dict) -> str:
    return Template(_TEMPLATE_PATH.read_text(encoding="utf-8")).substitute(params)


def render_settings(settings: dict) -> str:
    return render_params(resolve_params(settings))


def regenerate(settings: dict, css_path: Path | None = None) -> Path:
    """Write style.css for the configured appearance."""
    if css_path is None:
        css_path = ensure_config_dir() / "style.css"
    css_path.parent.mkdir(parents=True, exist_ok=True)
    css_path.write_text(render_settings(settings), encoding="utf-8")
    return css_path


def save_appearance(settings_path: Path, appearance: dict) -> dict:
    """Validate, persist [appearance], and re-render the stylesheet."""
    unknown = set(appearance) - APPEARANCE_KEYS
    if unknown:
        raise ValueError(f"unknown appearance keys: {sorted(unknown)}")
    doc = configstore.load_document(settings_path)
    table = doc.setdefault("appearance", {})
    for key, value in appearance.items():
        table[key] = value
    configstore.save_document(settings_path, doc)

    import tomllib

    with settings_path.open("rb") as f:
        settings = tomllib.load(f)
    regenerate(settings, settings_path.parent / "style.css")
    return resolve_params(settings)
