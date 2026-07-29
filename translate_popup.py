#!/usr/bin/env python3
"""translate-popup — OCR + translate card on GTK4 layer-shell.

Usage:
    ./run.sh --ocr                          screenshot → OCR → popup
    ./run.sh --ocr --translate              screenshot → OCR → translate → popup
    ./run.sh --translate --text "hello"     translate given text → popup
    ./run.sh --translate --from auto --to zh --text "…"
    ./run.sh --text "hello"                 show given text only
    ./run.sh                                demo mode
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gtk, Gtk4LayerShell, GLib, Gdk

# ── constants ────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_DIR = PROJECT_ROOT / "config"
CACHE_DIR = PROJECT_ROOT / ".cache"

OLLAMA_URL = os.environ.get(
    "TRANSLATE_OLLAMA_URL", "http://mario:11434/api/generate"
)
OLLAMA_MODEL = os.environ.get("TRANSLATE_OLLAMA_MODEL", "qwen2.5:3b")
STREAM_UI_INTERVAL_S = 0.04  # coalesce token UI updates (~25 fps)

# Upper bounds for each text section's scroll height (px), before the
# available-screen-space clamp in compute_section_caps() kicks in.
SOURCE_MAX_H = 280
TRANSLATION_MAX_H = 500

# Rough chrome heights (px) used only to estimate how much vertical room
# the popup's non-text chrome will consume, so we can reserve the rest of
# the available screen space for the text sections without overflowing.
HEADER_H = 56
FOOTER_H = 40
BODY_PAD_V = 28
SECTION_LABEL_H = 22
LANG_BAR_H = 46
SEPARATOR_H = 24

# Bob-like language pair. Codes used in CLI / dropdowns.
# "auto" is virtual: resolve from text (CJK vs Latin).
LANGUAGES: dict[str, dict[str, str]] = {
    "en": {
        "label": "English",
        "short": "英",
        "prompt": "English",
    },
    "zh": {
        "label": "中文",
        "short": "中",
        "prompt": "Chinese (Simplified)",
    },
}
SOURCE_CHOICES = ("auto", *LANGUAGES.keys())
TARGET_CHOICES = ("auto", *LANGUAGES.keys())

REQUIRED_BINS = {
    "ocr": ("slurp", "grim", "tesseract"),
    "position": ("hyprctl",),
}

_CJK_RE = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff"
    r"\U00020000-\U0002a6df]"
)
_LATIN_RE = re.compile(r"[A-Za-z]")
_BLANK_LINES_RE = re.compile(r"\n[ \t]*\n+")


# ── helpers ──────────────────────────────────────────────────────────────────
def _emit(callback, *args):
    """Trampoline for GLib.idle_add — returns False so it only fires once."""
    callback(*args)
    return False


def normalize_text(text: str) -> str:
    """Collapse OCR/LLM blank-line artifacts into single line breaks.

    Tesseract emits a blank line after almost every recognized line
    (each short UI string is treated as its own paragraph block), and
    some models pepper their output with extra blank lines. Neither is
    useful in a compact popup, so collapse any run of blank lines to a
    single newline.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _BLANK_LINES_RE.sub("\n", text)
    return text.strip()


def check_dependencies(need_ocr: bool) -> None:
    """Exit with a clear message if required system binaries are missing."""
    missing: list[str] = []
    for group in (("position",), ("ocr",) if need_ocr else ()):
        for name in group:
            for binary in REQUIRED_BINS[name]:
                if shutil.which(binary) is None:
                    missing.append(binary)
    seen: set[str] = set()
    unique = [b for b in missing if not (b in seen or seen.add(b))]
    if unique:
        print(
            "缺少依赖命令: " + ", ".join(unique) + "\n"
            "请安装对应包后再运行（Arch 示例: grim slurp tesseract "
            "tesseract-data-chi_sim tesseract-data-eng hyprland）。",
            file=sys.stderr,
        )
        sys.exit(1)


# ── language detection / resolution ──────────────────────────────────────────
def detect_lang(text: str) -> str:
    """Heuristic: more CJK → zh, else en (Bob-style auto for en↔zh)."""
    cjk = len(_CJK_RE.findall(text))
    latin = len(_LATIN_RE.findall(text))
    if cjk == 0 and latin == 0:
        return "en"
    return "zh" if cjk > latin else "en"


def opposite_lang(code: str) -> str:
    return "zh" if code == "en" else "en"


@dataclass(frozen=True)
class LangPair:
    source: str          # resolved concrete code (never auto)
    target: str          # resolved concrete code (never auto)
    detected: str        # detect_lang() result
    source_choice: str   # user selection (may be auto)
    target_choice: str   # user selection (may be auto)


def resolve_pair(text: str, source_choice: str, target_choice: str) -> LangPair:
    """Resolve auto selections into a concrete source→target pair."""
    detected = detect_lang(text)
    source = detected if source_choice == "auto" else source_choice
    if target_choice == "auto":
        target = opposite_lang(source)
    else:
        target = target_choice
    if source == target:
        target = opposite_lang(source)
    return LangPair(
        source=source,
        target=target,
        detected=detected,
        source_choice=source_choice,
        target_choice=target_choice,
    )


def choice_label(code: str, *, detected: str | None = None) -> str:
    if code == "auto":
        if detected and detected in LANGUAGES:
            return f"自动 · {LANGUAGES[detected]['short']}"
        return "自动"
    return LANGUAGES[code]["label"]


def build_prompt(text: str, pair: LangPair) -> str:
    src = LANGUAGES[pair.source]["prompt"]
    tgt = LANGUAGES[pair.target]["prompt"]
    return (
        "You are a professional translator. Translate the following text "
        f"from {src} to {tgt}. Output ONLY the translation, nothing else — "
        "no explanations, no notes, no quotation marks.\n\n"
        + text
    )


# ── cursor position ──────────────────────────────────────────────────────────
def get_cursor_position() -> tuple[int, int]:
    """Return (x, y) of the mouse cursor via hyprctl."""
    out = subprocess.check_output(["hyprctl", "cursorpos"], text=True).strip()
    x_str, y_str = out.split(",")
    return int(x_str.strip()), int(y_str.strip())


def get_screen_size() -> tuple[int, int]:
    """Return (width, height) of the active monitor via hyprctl."""
    raw = subprocess.check_output(["hyprctl", "monitors", "-j"], text=True)
    monitors = json.loads(raw)
    for m in monitors:
        if m.get("focused"):
            return m["width"], m["height"]
    if monitors:
        return monitors[0]["width"], monitors[0]["height"]
    return 1920, 1080


@dataclass(frozen=True)
class Placement:
    horizontal: str  # "right" | "left" — which side of the cursor we grow
    vertical: str    # "below" | "above" — which side of the cursor we grow
    left_margin: int
    top_margin: int    # only meaningful when vertical == "below"
    bottom_margin: int  # only meaningful when vertical == "above"
    avail_h: int        # usable vertical space in the chosen direction


def compute_placement(
    cursor_x: int,
    cursor_y: int,
    win_w: int = 480,
    gap: int = 12,
) -> Placement:
    """Decide which corner of the cursor to grow the popup into.

    Rather than guessing a fixed popup height and flipping if it would
    overflow (fragile once content can grow after the fact — e.g. a long
    translation), anchor to whichever vertical direction has more room and
    let the caller clamp content height to what's actually available. The
    layer-shell anchor keeps that edge pinned as the popup grows/shrinks.
    """
    sw, sh = get_screen_size()

    left = cursor_x + gap
    horizontal = "right"
    if left + win_w > sw:
        horizontal = "left"
        left = max(gap, cursor_x - win_w - gap)

    space_below = max(sh - cursor_y - gap, 0)
    space_above = max(cursor_y - gap, 0)
    if space_below >= space_above:
        vertical = "below"
        avail_h = space_below
    else:
        vertical = "above"
        avail_h = space_above
    avail_h = max(avail_h, 160)  # keep a usable minimum even near an edge

    return Placement(
        horizontal=horizontal,
        vertical=vertical,
        left_margin=left,
        top_margin=cursor_y + gap,
        bottom_margin=max(gap, sh - cursor_y + gap),
        avail_h=avail_h,
    )


def compute_section_caps(avail_h: int, translate: bool) -> tuple[int, int]:
    """Split available vertical space between the source/translation
    scroll areas so the whole popup fits without overflowing the screen.

    Returns (source_max_h, translation_max_h); translation_max_h is 0 when
    not in translate mode.
    """
    chrome = HEADER_H + FOOTER_H + BODY_PAD_V + SECTION_LABEL_H
    if translate:
        chrome += LANG_BAR_H + SEPARATOR_H + SECTION_LABEL_H

    budget = max(avail_h - chrome, 120)

    if not translate:
        return min(SOURCE_MAX_H, budget), 0

    source_cap = max(min(SOURCE_MAX_H, int(budget * 0.4)), 60)
    translation_cap = max(min(TRANSLATION_MAX_H, budget - source_cap), 80)
    return source_cap, translation_cap


def capture_region() -> Path:
    """Interactive region select → grim screenshot. Returns PNG path."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    screenshot = CACHE_DIR / "screenshot.png"

    try:
        coords = subprocess.check_output(["slurp", "-d"], text=True).strip()
    except subprocess.CalledProcessError:
        sys.exit(0)  # user cancelled (Escape)

    subprocess.run(["grim", "-g", coords, str(screenshot)], check=True)
    return screenshot


def ocr_image(screenshot: Path) -> str:
    """Run tesseract on an image path. Returns recognized text."""
    try:
        result = subprocess.check_output(
            ["tesseract", str(screenshot), "-", "-l", "chi_sim+eng"],
            text=True,
        ).strip()
    except subprocess.CalledProcessError:
        return "[OCR 失败]"
    finally:
        screenshot.unlink(missing_ok=True)

    result = normalize_text(result)
    return result if result else "（未识别到文字）"


# ── translation (streaming) ──────────────────────────────────────────────────
def translate_stream(
    text: str,
    pair: LangPair,
    on_token,
    cancel: threading.Event,
):
    """Run Ollama translation in a daemon thread."""
    prompt = build_prompt(text, pair)

    def _worker():
        full = ""
        last_emit = 0.0
        try:
            resp = requests.post(
                OLLAMA_URL,
                json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": True},
                stream=True,
                timeout=120,
            )
            resp.raise_for_status()
            for line in resp.iter_lines(decode_unicode=True):
                if cancel.is_set():
                    break
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                full += chunk.get("response", "")
                now = time.monotonic()
                done = bool(chunk.get("done"))
                if done or (now - last_emit) >= STREAM_UI_INTERVAL_S:
                    last_emit = now
                    GLib.idle_add(_emit, on_token, normalize_text(full))
                if done:
                    break
            else:
                if full and not cancel.is_set():
                    GLib.idle_add(_emit, on_token, normalize_text(full))
        except Exception as exc:
            if not cancel.is_set():
                GLib.idle_add(_emit, on_token, f"[翻译失败: {exc}]")

    threading.Thread(target=_worker, daemon=True).start()


def sync_scroll_height(
    label: Gtk.Label,
    scroll: Gtk.ScrolledWindow,
    minimum: int,
    maximum: int,
):
    """Set a scroller from the wrapped label's actual Pango layout height."""

    def _sync():
        _width, text_height = label.get_layout().get_pixel_size()
        content_height = max(minimum, min(text_height + 8, maximum))
        scroll.set_min_content_height(-1)
        scroll.set_max_content_height(-1)
        scroll.set_max_content_height(content_height)
        scroll.set_min_content_height(content_height)
        return False

    GLib.idle_add(_sync)


def _make_lang_dropdown(choices: tuple[str, ...], selected: str) -> Gtk.DropDown:
    labels = [choice_label(c) for c in choices]
    store = Gtk.StringList.new(labels)
    dropdown = Gtk.DropDown.new(store, None)
    try:
        dropdown.set_selected(choices.index(selected))
    except ValueError:
        dropdown.set_selected(0)
    dropdown.set_css_classes(["lang-dropdown"])
    return dropdown


# ── window ───────────────────────────────────────────────────────────────────
class TranslateWindow(Gtk.ApplicationWindow):
    def __init__(
        self,
        app: Gtk.Application,
        source_text: str,
        translate: bool = False,
        pending_ocr: Path | None = None,
        from_lang: str = "auto",
        to_lang: str = "auto",
    ):
        super().__init__(application=app, title="翻译")
        self._source_text = source_text
        self._translate = translate
        self._pending_ocr = pending_ocr
        self._from_lang = from_lang
        self._to_lang = to_lang
        self._closed = False
        self._cancel = threading.Event()
        self._gen = 0
        self._updating_lang_ui = False
        self._source_label: Gtk.Label | None = None
        self._source_scroll: Gtk.ScrolledWindow | None = None
        self._source_section_label: Gtk.Label | None = None
        self._translation_label: Gtk.Label | None = None
        self._translation_scroll: Gtk.ScrolledWindow | None = None
        self._translation_section_label: Gtk.Label | None = None
        self._from_dropdown: Gtk.DropDown | None = None
        self._to_dropdown: Gtk.DropDown | None = None
        self._pair_hint: Gtk.Label | None = None
        self._source_max_h = SOURCE_MAX_H
        self._translation_max_h = TRANSLATION_MAX_H

        self._setup_layer_shell()

        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_controller)
        self.connect("close-request", self._on_close_request)

        self._build_ui()

    def _setup_layer_shell(self):
        Gtk4LayerShell.init_for_window(self)
        Gtk4LayerShell.set_layer(self, Gtk4LayerShell.Layer.OVERLAY)
        Gtk4LayerShell.set_keyboard_mode(
            self, Gtk4LayerShell.KeyboardMode.EXCLUSIVE
        )

        try:
            cx, cy = get_cursor_position()
            placement = compute_placement(cx, cy)

            Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.LEFT, True)
            Gtk4LayerShell.set_margin(
                self, Gtk4LayerShell.Edge.LEFT, placement.left_margin
            )

            if placement.vertical == "below":
                Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.TOP, True)
                Gtk4LayerShell.set_margin(
                    self, Gtk4LayerShell.Edge.TOP, placement.top_margin
                )
            else:
                Gtk4LayerShell.set_anchor(
                    self, Gtk4LayerShell.Edge.BOTTOM, True
                )
                Gtk4LayerShell.set_margin(
                    self, Gtk4LayerShell.Edge.BOTTOM, placement.bottom_margin
                )

            self._source_max_h, self._translation_max_h = (
                compute_section_caps(placement.avail_h, self._translate)
            )
        except Exception:
            Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.TOP, True)
            Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.RIGHT, True)
            Gtk4LayerShell.set_margin(self, Gtk4LayerShell.Edge.TOP, 20)
            Gtk4LayerShell.set_margin(self, Gtk4LayerShell.Edge.RIGHT, 20)

    def _on_key_pressed(self, _controller, keyval, _keycode, _state):
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    def _on_close_request(self, *_args):
        self._closed = True
        self._cancel.set()
        return False

    def _current_pair(self) -> LangPair:
        return resolve_pair(self._source_text, self._from_lang, self._to_lang)

    def _build_ui(self):
        css_path = CONFIG_DIR / "style.css"
        if css_path.exists():
            css_provider = Gtk.CssProvider()
            css_provider.load_from_path(str(css_path))
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(),
                css_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.set_css_classes(["card"])
        self.set_child(root)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        header.set_css_classes(["header"])

        title = Gtk.Label(label="翻译")
        title.set_css_classes(["title"])
        title.set_halign(Gtk.Align.START)
        title.set_hexpand(True)

        close_btn = Gtk.Button(label="✕")
        close_btn.set_css_classes(["close-btn"])
        close_btn.connect("clicked", lambda _b: self.close())

        header.append(title)
        header.append(close_btn)
        root.append(header)

        if self._translate:
            root.append(self._build_lang_bar())

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        body.set_css_classes(["body"])
        root.append(body)

        self._source_label, self._source_scroll, self._source_section_label = (
            self._add_text_section(
                body,
                section="原文",
                text=self._source_text,
                css_class="source-text",
                min_h=30,
                max_h=self._source_max_h,
            )
        )

        if self._translate:
            sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep.set_css_classes(["separator"])
            body.append(sep)

            waiting = "等待识别..." if self._pending_ocr else "翻译中..."
            (
                self._translation_label,
                self._translation_scroll,
                self._translation_section_label,
            ) = self._add_text_section(
                body,
                section="译文",
                text=waiting,
                css_class="translation-text",
                min_h=36,
                max_h=self._translation_max_h,
            )
            self._refresh_pair_labels()

        footer_box = Gtk.Box()
        footer_box.set_css_classes(["footer"])
        if self._translate:
            model_name = f"Ollama · {OLLAMA_MODEL}"
        else:
            model_name = "OCR · tesseract"
        footer_label = Gtk.Label(label=model_name)
        footer_label.set_halign(Gtk.Align.END)
        footer_label.set_hexpand(True)
        footer_box.append(footer_label)
        root.append(footer_box)

        if self._pending_ocr:
            GLib.idle_add(self._start_ocr)
        elif self._translate:
            GLib.idle_add(self._start_translation)

    def _build_lang_bar(self) -> Gtk.Box:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.set_css_classes(["lang-bar"])

        self._from_dropdown = _make_lang_dropdown(SOURCE_CHOICES, self._from_lang)
        self._to_dropdown = _make_lang_dropdown(TARGET_CHOICES, self._to_lang)

        swap_btn = Gtk.Button(label="⇄")
        swap_btn.set_css_classes(["swap-btn"])
        swap_btn.set_tooltip_text("交换语言")
        swap_btn.connect("clicked", self._on_swap_clicked)

        self._pair_hint = Gtk.Label(label="")
        self._pair_hint.set_css_classes(["pair-hint"])
        self._pair_hint.set_halign(Gtk.Align.END)
        self._pair_hint.set_hexpand(True)

        self._from_dropdown.connect("notify::selected", self._on_from_changed)
        self._to_dropdown.connect("notify::selected", self._on_to_changed)

        bar.append(self._from_dropdown)
        bar.append(swap_btn)
        bar.append(self._to_dropdown)
        bar.append(self._pair_hint)
        return bar

    def _add_text_section(
        self,
        body: Gtk.Box,
        section: str,
        text: str,
        css_class: str,
        min_h: int,
        max_h: int,
    ) -> tuple[Gtk.Label, Gtk.ScrolledWindow, Gtk.Label]:
        section_label = Gtk.Label(label=section)
        section_label.set_css_classes(["section-label"])
        section_label.set_halign(Gtk.Align.START)
        section_label.set_xalign(0)
        body.append(section_label)

        label = Gtk.Label(label=text)
        label.set_wrap(True)
        label.set_selectable(True)
        label.set_xalign(0)
        label.set_css_classes([css_class])

        scroll = Gtk.ScrolledWindow()
        scroll.set_child(label)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        body.append(scroll)
        sync_scroll_height(label, scroll, min_h, max_h)
        return label, scroll, section_label

    def _refresh_pair_labels(self):
        if not self._translate:
            return
        pair = self._current_pair()
        if self._source_section_label:
            src_name = LANGUAGES[pair.source]["label"]
            if pair.source_choice == "auto":
                self._source_section_label.set_label(f"原文 · {src_name}（自动）")
            else:
                self._source_section_label.set_label(f"原文 · {src_name}")
        if self._translation_section_label:
            tgt_name = LANGUAGES[pair.target]["label"]
            self._translation_section_label.set_label(f"译文 · {tgt_name}")
        if self._pair_hint:
            self._pair_hint.set_label(
                f"{LANGUAGES[pair.source]['short']} → "
                f"{LANGUAGES[pair.target]['short']}"
            )
        # Refresh "自动 · 英/中" labels on dropdowns without firing change handlers
        self._updating_lang_ui = True
        try:
            if self._from_dropdown is not None:
                model = self._from_dropdown.get_model()
                if isinstance(model, Gtk.StringList):
                    for i, code in enumerate(SOURCE_CHOICES):
                        det = pair.detected if code == "auto" else None
                        model.splice(i, 1, [choice_label(code, detected=det)])
            if self._to_dropdown is not None:
                model = self._to_dropdown.get_model()
                if isinstance(model, Gtk.StringList):
                    for i, code in enumerate(TARGET_CHOICES):
                        det = (
                            opposite_lang(pair.source) if code == "auto" else None
                        )
                        model.splice(i, 1, [choice_label(code, detected=det)])
        finally:
            self._updating_lang_ui = False

    def _on_from_changed(self, dropdown, _pspec):
        if self._updating_lang_ui:
            return
        idx = dropdown.get_selected()
        if idx < 0 or idx >= len(SOURCE_CHOICES):
            return
        new = SOURCE_CHOICES[idx]
        if new == self._from_lang:
            return
        self._from_lang = new
        self._on_lang_pair_changed()

    def _on_to_changed(self, dropdown, _pspec):
        if self._updating_lang_ui:
            return
        idx = dropdown.get_selected()
        if idx < 0 or idx >= len(TARGET_CHOICES):
            return
        new = TARGET_CHOICES[idx]
        if new == self._to_lang:
            return
        self._to_lang = new
        self._on_lang_pair_changed()

    def _on_swap_clicked(self, _btn):
        # Bob-style: swap concrete sides. If a side was auto, pin it to the
        # previously resolved language so the swap is meaningful.
        pair = self._current_pair()
        new_from = (
            pair.target if self._from_lang == "auto" else self._to_lang
        )
        new_to = (
            pair.source if self._to_lang == "auto" else self._from_lang
        )
        # If both were concrete, simple swap of choices
        if self._from_lang != "auto" and self._to_lang != "auto":
            new_from, new_to = self._to_lang, self._from_lang

        self._from_lang = new_from if new_from != "auto" else pair.target
        self._to_lang = new_to if new_to != "auto" else pair.source

        self._updating_lang_ui = True
        try:
            if self._from_dropdown is not None:
                self._from_dropdown.set_selected(
                    SOURCE_CHOICES.index(self._from_lang)
                )
            if self._to_dropdown is not None:
                self._to_dropdown.set_selected(
                    TARGET_CHOICES.index(self._to_lang)
                )
        finally:
            self._updating_lang_ui = False

        self._on_lang_pair_changed()

    def _on_lang_pair_changed(self):
        self._refresh_pair_labels()
        if self._pending_ocr:
            return  # wait until OCR finishes
        if self._translate and not self._is_placeholder_source():
            self._restart_translation()

    def _is_placeholder_source(self) -> bool:
        return self._source_text in ("识别中...", "等待识别...")

    def _start_ocr(self):
        path = self._pending_ocr
        assert path is not None

        def _worker():
            text = ocr_image(path)
            if not self._closed:
                GLib.idle_add(_emit, self._on_ocr_done, text)

        threading.Thread(target=_worker, daemon=True).start()
        return False

    def _on_ocr_done(self, text: str):
        if self._closed:
            return
        self._source_text = text
        self._pending_ocr = None
        if self._source_label and self._source_scroll:
            self._source_label.set_label(text)
            sync_scroll_height(
                self._source_label, self._source_scroll, 30, self._source_max_h
            )
        self._refresh_pair_labels()
        if self._translate:
            self._start_translation()

    def _restart_translation(self):
        self._cancel.set()
        self._cancel = threading.Event()
        if self._translation_label:
            self._translation_label.set_label("翻译中...")
        self._start_translation()

    def _start_translation(self):
        if self._closed or self._is_placeholder_source():
            return False
        self._gen += 1
        gen = self._gen
        pair = self._current_pair()
        self._refresh_pair_labels()

        def _on_token(full_result: str):
            if gen != self._gen:
                return
            self._on_token(full_result)

        translate_stream(
            self._source_text, pair, _on_token, self._cancel
        )
        return False

    def _on_token(self, full_result: str):
        if self._closed or not self._translation_label:
            return
        self._translation_label.set_label(full_result)
        if self._translation_scroll:
            sync_scroll_height(
                self._translation_label,
                self._translation_scroll,
                36,
                self._translation_max_h,
            )


# ── application ──────────────────────────────────────────────────────────────
class TranslateApp(Gtk.Application):
    def __init__(
        self,
        source_text: str,
        translate: bool,
        pending_ocr: Path | None = None,
        from_lang: str = "auto",
        to_lang: str = "auto",
    ):
        super().__init__(application_id="com.translate.tool")
        self._source_text = source_text
        self._translate = translate
        self._pending_ocr = pending_ocr
        self._from_lang = from_lang
        self._to_lang = to_lang

    def do_activate(self):
        win = TranslateWindow(
            self,
            self._source_text,
            self._translate,
            pending_ocr=self._pending_ocr,
            from_lang=self._from_lang,
            to_lang=self._to_lang,
        )
        win.present()


# ── cli ──────────────────────────────────────────────────────────────────────
def main():
    if CACHE_DIR.exists():
        for f in CACHE_DIR.iterdir():
            f.unlink(missing_ok=True)

    lang_choices = list(SOURCE_CHOICES)
    parser = argparse.ArgumentParser(description="翻译弹窗")
    parser.add_argument("--ocr", action="store_true", help="截图 OCR 识别文字")
    parser.add_argument(
        "--translate", action="store_true", help="调用 Ollama 翻译"
    )
    parser.add_argument("--text", type=str, default=None, help="直接指定文本")
    parser.add_argument(
        "--from",
        dest="from_lang",
        choices=lang_choices,
        default=os.environ.get("TRANSLATE_FROM", "auto"),
        help="源语言（默认 auto：按中英字符占比判定）",
    )
    parser.add_argument(
        "--to",
        dest="to_lang",
        choices=lang_choices,
        default=os.environ.get("TRANSLATE_TO", "auto"),
        help="目标语言（默认 auto：取源语言的对面）",
    )
    args = parser.parse_args()

    check_dependencies(need_ocr=args.ocr)

    pending_ocr: Path | None = None
    if args.ocr:
        screenshot = capture_region()
        source_text = "识别中..."
        pending_ocr = screenshot
    elif args.text:
        source_text = normalize_text(args.text)
    else:
        source_text = (
            "The quick brown fox jumps over the lazy dog.\n\n"
            "使用方法：\n"
            "  ./run.sh --ocr --translate\n"
            "  ./run.sh --translate --text …\n"
            "  ./run.sh --translate --from auto --to zh --text …\n"
            "  ./run.sh --ocr\n\n"
            "语言：弹窗内可切换 / 交换；auto 按中英占比双向翻译。\n"
            "环境变量：TRANSLATE_OLLAMA_URL / TRANSLATE_OLLAMA_MODEL / "
            "TRANSLATE_FROM / TRANSLATE_TO"
        )

    app = TranslateApp(
        source_text,
        translate=args.translate,
        pending_ocr=pending_ocr,
        from_lang=args.from_lang,
        to_lang=args.to_lang,
    )
    return app.run()


if __name__ == "__main__":
    main()
