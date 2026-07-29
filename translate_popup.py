#!/usr/bin/env python3
"""translate-popup — OCR + translate card on GTK4 layer-shell.

Usage:
    ./run.sh --ocr                          screenshot → OCR → popup
    ./run.sh --ocr --translate              screenshot → OCR → translate → popup
    ./run.sh --translate --text "hello"     translate given text → popup
    ./run.sh --text "hello"                 show given text only
    ./run.sh                                demo mode
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
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
AUTO_CLOSE_TRANSLATE_S = int(os.environ.get("TRANSLATE_AUTO_CLOSE_S", "30"))
AUTO_CLOSE_OCR_S = int(os.environ.get("TRANSLATE_AUTO_CLOSE_OCR_S", "8"))
STREAM_UI_INTERVAL_S = 0.04  # coalesce token UI updates (~25 fps)

TRANSLATION_PROMPT_PREFIX = (
    "You are a professional translator. Translate the following text to "
    "Chinese (Simplified). Output ONLY the translation, nothing else — "
    "no explanations, no notes, no quotation marks.\n\n"
)

REQUIRED_BINS = {
    "ocr": ("slurp", "grim", "tesseract"),
    "position": ("hyprctl",),
}


# ── helpers ──────────────────────────────────────────────────────────────────
def _emit(callback, *args):
    """Trampoline for GLib.idle_add — returns False so it only fires once."""
    callback(*args)
    return False


def check_dependencies(need_ocr: bool) -> None:
    """Exit with a clear message if required system binaries are missing."""
    missing: list[str] = []
    for group in (("position",), ("ocr",) if need_ocr else ()):
        for name in group:
            for binary in REQUIRED_BINS[name]:
                if shutil.which(binary) is None:
                    missing.append(binary)
    # de-dupe while preserving order
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


def compute_margins(
    cursor_x: int,
    cursor_y: int,
    win_w: int = 480,
    win_h: int = 500,
    gap: int = 12,
) -> dict[str, int]:
    """Place the popup near the cursor without overflowing the screen."""
    sw, sh = get_screen_size()

    left = cursor_x + gap
    top = cursor_y + gap

    if left + win_w > sw:
        left = cursor_x - win_w - gap
        if left < 0:
            left = gap

    if top + win_h > sh:
        top = cursor_y - win_h - gap
        if top < 0:
            top = gap

    return {"left": left, "top": top}


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

    return result if result else "（未识别到文字）"


# ── translation (streaming) ──────────────────────────────────────────────────
def translate_stream(text: str, on_token, cancel: threading.Event):
    """Run Ollama translation in a daemon thread.

    Calls on_token(full_result) from the main thread via GLib.idle_add.
    UI updates are coalesced to ~STREAM_UI_INTERVAL_S.
    Stops early when cancel is set.
    """
    prompt = TRANSLATION_PROMPT_PREFIX + text

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
                    GLib.idle_add(_emit, on_token, full)
                if done:
                    break
            else:
                # stream ended without done — flush remaining text
                if full and not cancel.is_set():
                    GLib.idle_add(_emit, on_token, full)
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
        # Clear previous bounds first so min never exceeds max mid-update.
        scroll.set_min_content_height(-1)
        scroll.set_max_content_height(-1)
        scroll.set_max_content_height(content_height)
        scroll.set_min_content_height(content_height)
        return False

    GLib.idle_add(_sync)


# ── window ───────────────────────────────────────────────────────────────────
class TranslateWindow(Gtk.ApplicationWindow):
    def __init__(
        self,
        app: Gtk.Application,
        source_text: str,
        translate: bool = False,
        pending_ocr: Path | None = None,
    ):
        super().__init__(application=app, title="翻译")
        self._source_text = source_text
        self._translate = translate
        self._pending_ocr = pending_ocr
        self._closed = False
        self._cancel = threading.Event()
        self._source_label: Gtk.Label | None = None
        self._source_scroll: Gtk.ScrolledWindow | None = None
        self._translation_label: Gtk.Label | None = None
        self._translation_scroll: Gtk.ScrolledWindow | None = None

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
            margins = compute_margins(cx, cy)
            Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.TOP, True)
            Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.LEFT, True)
            Gtk4LayerShell.set_margin(
                self, Gtk4LayerShell.Edge.TOP, margins["top"]
            )
            Gtk4LayerShell.set_margin(
                self, Gtk4LayerShell.Edge.LEFT, margins["left"]
            )
        except Exception:
            # Fallback: pin to top-right corner
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
        return False  # allow default close

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

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        body.set_css_classes(["body"])
        root.append(body)

        self._source_label, self._source_scroll = self._add_text_section(
            body,
            section="原文",
            text=self._source_text,
            css_class="source-text",
            min_h=30,
            max_h=280,
        )

        if self._translate:
            sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep.set_css_classes(["separator"])
            body.append(sep)

            waiting = "等待识别..." if self._pending_ocr else "翻译中..."
            self._translation_label, self._translation_scroll = (
                self._add_text_section(
                    body,
                    section="译文",
                    text=waiting,
                    css_class="translation-text",
                    min_h=36,
                    max_h=500,
                )
            )

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

    def _add_text_section(
        self,
        body: Gtk.Box,
        section: str,
        text: str,
        css_class: str,
        min_h: int,
        max_h: int,
    ) -> tuple[Gtk.Label, Gtk.ScrolledWindow]:
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
        return label, scroll

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
        if self._source_label and self._source_scroll:
            self._source_label.set_label(text)
            sync_scroll_height(
                self._source_label, self._source_scroll, 30, 280
            )
        if self._translate:
            self._start_translation()

    def _start_translation(self):
        if self._closed:
            return False
        translate_stream(self._source_text, self._on_token, self._cancel)
        return False

    def _on_token(self, full_result: str):
        if self._closed or not self._translation_label:
            return
        self._translation_label.set_label(full_result)
        if self._translation_scroll:
            sync_scroll_height(
                self._translation_label, self._translation_scroll, 36, 500
            )


# ── application ──────────────────────────────────────────────────────────────
class TranslateApp(Gtk.Application):
    def __init__(
        self,
        source_text: str,
        translate: bool,
        pending_ocr: Path | None = None,
    ):
        super().__init__(application_id="com.translate.tool")
        self._source_text = source_text
        self._translate = translate
        self._pending_ocr = pending_ocr

    def do_activate(self):
        win = TranslateWindow(
            self,
            self._source_text,
            self._translate,
            pending_ocr=self._pending_ocr,
        )
        win.present()
        delay = AUTO_CLOSE_TRANSLATE_S if self._translate else AUTO_CLOSE_OCR_S

        def _auto_close():
            win.close()
            return False  # one-shot

        GLib.timeout_add_seconds(delay, _auto_close)


# ── cli ──────────────────────────────────────────────────────────────────────
def main():
    if CACHE_DIR.exists():
        for f in CACHE_DIR.iterdir():
            f.unlink(missing_ok=True)

    parser = argparse.ArgumentParser(description="翻译弹窗")
    parser.add_argument("--ocr", action="store_true", help="截图 OCR 识别文字")
    parser.add_argument(
        "--translate", action="store_true", help="调用 Ollama 翻译"
    )
    parser.add_argument("--text", type=str, default=None, help="直接指定文本")
    args = parser.parse_args()

    check_dependencies(need_ocr=args.ocr)

    pending_ocr: Path | None = None
    if args.ocr:
        # Region select blocks (needs a free screen); OCR runs after the popup
        # is shown so the user sees "识别中..." instead of a blank wait.
        screenshot = capture_region()
        source_text = "识别中..."
        pending_ocr = screenshot
    elif args.text:
        source_text = args.text
    else:
        source_text = (
            "The quick brown fox jumps over the lazy dog.\n\n"
            "使用方法：\n"
            "  ./run.sh --ocr --translate    截图 → OCR → 翻译\n"
            "  ./run.sh --translate --text …  翻译指定文本\n"
            "  ./run.sh --ocr                 仅 OCR 识别\n\n"
            "环境变量：TRANSLATE_OLLAMA_URL / TRANSLATE_OLLAMA_MODEL"
        )

    app = TranslateApp(
        source_text, translate=args.translate, pending_ocr=pending_ocr
    )
    return app.run()


if __name__ == "__main__":
    main()
