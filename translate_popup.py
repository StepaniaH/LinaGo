#!/usr/bin/env python3
"""translate-popup — OCR + translate card on GTK4 layer-shell.

Usage:
    ./run.sh --ocr                          screenshot → OCR → popup
    ./run.sh --ocr --translate              screenshot → OCR → translate → popup
    ./run.sh --translate --text "hello"     translate given text → popup
    ./run.sh --translate --provider openai --text "…"
    ./run.sh --translate --from auto --to zh --text "…"
    ./run.sh --text "hello"                 show given text only
    ./run.sh                                demo mode

Providers are configured in config/settings.toml (Ollama + OpenAI-compatible
BYOK). API keys go in config/secrets.toml (see secrets.toml.example).
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
from dataclasses import dataclass
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gtk, Gtk4LayerShell, GLib, Gdk

from providers import AppConfig, Provider, load_config, stream_completion

# ── constants ────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_DIR = PROJECT_ROOT / "config"
CACHE_DIR = PROJECT_ROOT / ".cache"

SOURCE_EDIT_DEBOUNCE_MS = 700  # wait for typing to settle before retranslating

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
    provider: Provider,
    text: str,
    pair: LangPair,
    on_token,
    cancel: threading.Event,
):
    """Run translation via the active provider in a daemon thread."""
    prompt = build_prompt(text, pair)

    def _worker():
        def _ui_token(full: str):
            GLib.idle_add(_emit, on_token, normalize_text(full))

        try:
            stream_completion(provider, prompt, _ui_token, cancel)
        except Exception as exc:
            if not cancel.is_set():
                GLib.idle_add(_emit, on_token, f"[翻译失败: {exc}]")

    threading.Thread(target=_worker, daemon=True).start()


def sync_scroll_height(
    widget: Gtk.Widget,
    scroll: Gtk.ScrolledWindow,
    minimum: int,
    maximum: int,
):
    """Size a scroller to its child's natural text height."""

    def _sync():
        if isinstance(widget, Gtk.Label):
            _width, text_height = widget.get_layout().get_pixel_size()
        else:
            # TextView: ask for the natural height at the current width.
            width = widget.get_width() or scroll.get_width() or 0
            _min, text_height, _mb, _nb = widget.measure(
                Gtk.Orientation.VERTICAL, width or -1
            )
        content_height = max(minimum, min(text_height + 8, maximum))
        scroll.set_min_content_height(-1)
        scroll.set_max_content_height(-1)
        scroll.set_max_content_height(content_height)
        scroll.set_min_content_height(content_height)
        return False

    GLib.idle_add(_sync)


def copy_to_clipboard(widget: Gtk.Widget, text: str) -> bool:
    """Copy text to the Wayland clipboard.

    Prefers wl-copy: GTK's own clipboard offer dies with the popup, so a
    plain GDK copy would leave nothing to paste once the window closes.
    """
    if not text:
        return False

    if shutil.which("wl-copy"):
        try:
            subprocess.Popen(
                ["wl-copy", "--", text],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except OSError:
            pass

    display = widget.get_display()
    if display is None:
        return False
    display.get_clipboard().set(text)
    return True


class TextSection:
    """A labeled text area with a copy button in its bottom-right corner.

    Renders as a Gtk.Label normally, or an editable Gtk.TextView when
    editable=True (used for the source text so edits can retranslate).
    """

    def __init__(
        self,
        body: Gtk.Box,
        section: str,
        text: str,
        css_class: str,
        min_h: int,
        max_h: int,
        editable: bool = False,
    ):
        self.min_h = min_h
        self.max_h = max_h
        self.editable = editable
        self._copy_reset_id = 0

        self.section_label = Gtk.Label(label=section)
        self.section_label.set_css_classes(["section-label"])
        self.section_label.set_halign(Gtk.Align.START)
        self.section_label.set_xalign(0)
        body.append(self.section_label)

        if editable:
            self.view = Gtk.TextView()
            self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            self.view.set_accepts_tab(False)
            self.view.set_top_margin(2)
            self.view.set_bottom_margin(2)
            self.view.set_css_classes([css_class, "editable-text"])
            self.buffer = self.view.get_buffer()
            self.buffer.set_text(text)
        else:
            self.view = Gtk.Label(label=text)
            self.view.set_wrap(True)
            self.view.set_selectable(True)
            self.view.set_xalign(0)
            self.view.set_valign(Gtk.Align.START)
            self.view.set_css_classes([css_class])
            self.buffer = None

        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_child(self.view)
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.copy_btn = Gtk.Button(label="⧉")
        self.copy_btn.set_css_classes(["copy-btn"])
        self.copy_btn.set_tooltip_text("复制")
        self.copy_btn.set_halign(Gtk.Align.END)
        self.copy_btn.set_valign(Gtk.Align.END)
        self.copy_btn.set_can_focus(False)
        self.copy_btn.connect("clicked", self._on_copy_clicked)

        overlay = Gtk.Overlay()
        overlay.set_child(self.scroll)
        overlay.add_overlay(self.copy_btn)
        body.append(overlay)

        self.sync_height()

    def get_text(self) -> str:
        if self.buffer is not None:
            start, end = self.buffer.get_bounds()
            return self.buffer.get_text(start, end, False)
        return self.view.get_text()

    def set_text(self, text: str):
        if self.buffer is not None:
            self.buffer.set_text(text)
        else:
            self.view.set_label(text)
        self.sync_height()

    def set_section_label(self, text: str):
        self.section_label.set_label(text)

    def sync_height(self):
        sync_scroll_height(self.view, self.scroll, self.min_h, self.max_h)

    def _on_copy_clicked(self, _btn):
        if not copy_to_clipboard(self.copy_btn, self.get_text().strip()):
            return
        self.copy_btn.set_label("✓")
        if self._copy_reset_id:
            GLib.source_remove(self._copy_reset_id)

        def _reset():
            self._copy_reset_id = 0
            self.copy_btn.set_label("⧉")
            return False

        self._copy_reset_id = GLib.timeout_add(1200, _reset)


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
        config: AppConfig | None = None,
        provider_name: str | None = None,
    ):
        super().__init__(application=app, title="翻译")
        self._source_text = source_text
        self._translate = translate
        self._pending_ocr = pending_ocr
        self._from_lang = from_lang
        self._to_lang = to_lang
        self._config = config or load_config()
        self._provider_name = provider_name or self._config.active
        self._closed = False
        self._cancel = threading.Event()
        self._gen = 0
        self._updating_lang_ui = False
        self._updating_provider_ui = False
        self._updating_source_ui = False
        self._edit_timeout_id = 0
        self._source_section: TextSection | None = None
        self._translation_section: TextSection | None = None
        self._from_dropdown: Gtk.DropDown | None = None
        self._to_dropdown: Gtk.DropDown | None = None
        self._provider_dropdown: Gtk.DropDown | None = None
        self._footer_label: Gtk.Label | None = None
        self._source_max_h = SOURCE_MAX_H
        self._translation_max_h = TRANSLATION_MAX_H

        self._setup_layer_shell()

        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_controller)
        self.connect("close-request", self._on_close_request)

        self._build_ui()

    def _provider(self) -> Provider:
        return self._config.get(self._provider_name)

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
        if self._edit_timeout_id:
            GLib.source_remove(self._edit_timeout_id)
            self._edit_timeout_id = 0
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

        self._source_section = TextSection(
            body,
            section="原文",
            text=self._source_text,
            css_class="source-text",
            min_h=30,
            max_h=self._source_max_h,
            editable=True,
        )
        if self._source_section.buffer is not None:
            self._source_section.buffer.connect(
                "changed", self._on_source_changed
            )
        source_keys = Gtk.EventControllerKey()
        source_keys.connect("key-pressed", self._on_source_key_pressed)
        self._source_section.view.add_controller(source_keys)

        if self._translate:
            sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep.set_css_classes(["separator"])
            body.append(sep)

            waiting = "等待识别..." if self._pending_ocr else "翻译中..."
            self._translation_section = TextSection(
                body,
                section="译文",
                text=waiting,
                css_class="translation-text",
                min_h=36,
                max_h=self._translation_max_h,
            )
            self._refresh_pair_labels()

        footer_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer_box.set_css_classes(["footer"])
        if self._translate:
            names = self._config.names()
            labels = [self._config.get(n).display for n in names]
            store = Gtk.StringList.new(labels)
            self._provider_dropdown = Gtk.DropDown.new(store, None)
            try:
                self._provider_dropdown.set_selected(
                    names.index(self._provider_name)
                )
            except ValueError:
                self._provider_dropdown.set_selected(0)
                self._provider_name = names[0]
            self._provider_dropdown.set_css_classes(["provider-dropdown"])
            self._provider_dropdown.set_tooltip_text(
                "切换翻译后端（本地 Ollama / BYOK）"
            )
            self._provider_dropdown.connect(
                "notify::selected", self._on_provider_changed
            )
            footer_box.append(self._provider_dropdown)

            self._footer_label = Gtk.Label(label="")
            self._footer_label.set_halign(Gtk.Align.END)
            self._footer_label.set_hexpand(True)
            self._footer_label.set_css_classes(["footer-meta"])
            footer_box.append(self._footer_label)
            self._refresh_provider_footer()
        else:
            footer_label = Gtk.Label(label="OCR · tesseract")
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

        self._from_dropdown.connect("notify::selected", self._on_from_changed)
        self._to_dropdown.connect("notify::selected", self._on_to_changed)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)

        bar.append(self._from_dropdown)
        bar.append(swap_btn)
        bar.append(self._to_dropdown)
        bar.append(spacer)
        return bar

    def _refresh_pair_labels(self):
        if not self._translate:
            return
        pair = self._current_pair()
        if self._source_section:
            src_name = LANGUAGES[pair.source]["label"]
            if pair.source_choice == "auto":
                self._source_section.set_section_label(
                    f"原文 · {src_name}（自动）"
                )
            else:
                self._source_section.set_section_label(f"原文 · {src_name}")
        if self._translation_section:
            tgt_name = LANGUAGES[pair.target]["label"]
            self._translation_section.set_section_label(f"译文 · {tgt_name}")
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

    def _refresh_provider_footer(self):
        if not self._footer_label:
            return
        p = self._provider()
        kind = "本地" if p.type == "ollama" else "BYOK"
        self._footer_label.set_label(kind)

    def _on_provider_changed(self, dropdown, _pspec):
        if self._updating_provider_ui:
            return
        names = self._config.names()
        idx = dropdown.get_selected()
        if idx < 0 or idx >= len(names):
            return
        new = names[idx]
        if new == self._provider_name:
            return
        self._provider_name = new
        self._refresh_provider_footer()
        if self._pending_ocr:
            return
        if self._translate and not self._is_placeholder_source():
            self._restart_translation()

    def _is_placeholder_source(self) -> bool:
        return self._source_text in ("识别中...", "等待识别...")

    def _on_source_changed(self, _buffer):
        if self._updating_source_ui or self._closed:
            return
        if self._source_section:
            self._source_section.sync_height()
        if self._edit_timeout_id:
            GLib.source_remove(self._edit_timeout_id)
        self._edit_timeout_id = GLib.timeout_add(
            SOURCE_EDIT_DEBOUNCE_MS, self._commit_source_edit
        )

    def _on_source_key_pressed(self, _controller, keyval, _keycode, state):
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if ctrl and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if self._edit_timeout_id:
                GLib.source_remove(self._edit_timeout_id)
                self._edit_timeout_id = 0
            self._commit_source_edit()
            return True
        return False

    def _commit_source_edit(self):
        self._edit_timeout_id = 0
        if self._closed or not self._source_section:
            return False

        text = self._source_section.get_text().strip()
        if not text or text == self._source_text:
            return False

        self._source_text = text
        self._pending_ocr = None
        self._refresh_pair_labels()
        if self._translate:
            self._restart_translation()
        return False

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
        if self._source_section:
            self._updating_source_ui = True
            try:
                self._source_section.set_text(text)
            finally:
                self._updating_source_ui = False
        self._refresh_pair_labels()
        if self._translate:
            self._start_translation()

    def _restart_translation(self):
        self._cancel.set()
        self._cancel = threading.Event()
        if self._translation_section:
            self._translation_section.set_text("翻译中...")
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
            self._provider(), self._source_text, pair, _on_token, self._cancel
        )
        return False

    def _on_token(self, full_result: str):
        if self._closed or not self._translation_section:
            return
        self._translation_section.set_text(full_result)


# ── application ──────────────────────────────────────────────────────────────
class TranslateApp(Gtk.Application):
    def __init__(
        self,
        source_text: str,
        translate: bool,
        pending_ocr: Path | None = None,
        from_lang: str = "auto",
        to_lang: str = "auto",
        config: AppConfig | None = None,
        provider_name: str | None = None,
    ):
        super().__init__(application_id="com.translate.tool")
        self._source_text = source_text
        self._translate = translate
        self._pending_ocr = pending_ocr
        self._from_lang = from_lang
        self._to_lang = to_lang
        self._config = config or load_config()
        self._provider_name = provider_name or self._config.active

    def do_activate(self):
        win = TranslateWindow(
            self,
            self._source_text,
            self._translate,
            pending_ocr=self._pending_ocr,
            from_lang=self._from_lang,
            to_lang=self._to_lang,
            config=self._config,
            provider_name=self._provider_name,
        )
        win.present()


# ── cli ──────────────────────────────────────────────────────────────────────
def main():
    if CACHE_DIR.exists():
        for f in CACHE_DIR.iterdir():
            f.unlink(missing_ok=True)

    config = load_config()
    lang_choices = list(SOURCE_CHOICES)
    provider_choices = config.names()

    parser = argparse.ArgumentParser(description="翻译弹窗")
    parser.add_argument("--ocr", action="store_true", help="截图 OCR 识别文字")
    parser.add_argument(
        "--translate", action="store_true", help="调用当前 provider 翻译"
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
    parser.add_argument(
        "--provider",
        choices=provider_choices,
        default=None,
        help=(
            "翻译后端（默认读 config/settings.toml 的 [app].provider；"
            f"可用: {', '.join(provider_choices)}）"
        ),
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
        active = config.get(args.provider)
        source_text = (
            "The quick brown fox jumps over the lazy dog.\n\n"
            "使用方法：\n"
            "  ./run.sh --ocr --translate\n"
            "  ./run.sh --translate --text …\n"
            "  ./run.sh --translate --provider ollama --text …\n"
            "  ./run.sh --translate --provider openai --text …\n\n"
            f"当前后端：{active.display}（{active.type}）\n"
            "配置：config/settings.toml · 密钥：config/secrets.toml\n"
            "环境变量：TRANSLATE_PROVIDER / TRANSLATE_MODEL / "
            "TRANSLATE_OLLAMA_URL / TRANSLATE_FROM / TRANSLATE_TO"
        )

    app = TranslateApp(
        source_text,
        translate=args.translate,
        pending_ocr=pending_ocr,
        from_lang=args.from_lang,
        to_lang=args.to_lang,
        config=config,
        provider_name=args.provider or config.active,
    )
    return app.run()


if __name__ == "__main__":
    main()
