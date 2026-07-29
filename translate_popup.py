#!/usr/bin/env python3
"""translate-popup — OCR + translate card on GTK4 layer-shell.

Usage:
    ./run.sh --ocr                          screenshot → OCR → popup
    ./run.sh --ocr --translate              screenshot → OCR → translate → popup
    ./run.sh --translate --text "hello"     translate given text → popup
    ./run.sh --text "hello"                 show given text only
    ./run.sh                                demo mode
"""

import argparse
import json
import subprocess
import sys
import threading
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

OLLAMA_URL = "http://mario:11434/api/generate"
OLLAMA_MODEL = "qwen2.5:3b"

TRANSLATION_PROMPT = (
    "You are a professional translator. Translate the following text to "
    "Chinese (Simplified). Output ONLY the translation, nothing else — "
    "no explanations, no notes, no quotation marks.\n\n{text}"
)


# ── cursor position ──────────────────────────────────────────────────────────
def get_cursor_position() -> tuple[int, int]:
    """Return (x, y) of the mouse cursor via hyprctl."""
    out = subprocess.check_output(["hyprctl", "cursorpos"], text=True).strip()
    x_str, y_str = out.split(",")
    return int(x_str.strip()), int(y_str.strip())


def get_screen_size() -> tuple[int, int]:
    """Return (width, height) of the active monitor via hyprctl."""
    raw = subprocess.check_output(
        ["hyprctl", "monitors", "-j"], text=True
    )
    monitors = json.loads(raw)
    # pick the first focused monitor; fall back to the first one
    for m in monitors:
        if m.get("focused"):
            return m["width"], m["height"]
    if monitors:
        return monitors[0]["width"], monitors[0]["height"]
    return 1920, 1080  # fallback


def compute_margins(
    cursor_x: int,
    cursor_y: int,
    win_w: int = 480,
    win_h: int = 500,
    gap: int = 12,
) -> dict:
    """Given cursor position, return layer-shell margin dict
    that places the popup near the cursor without overflowing the screen."""
    sw, sh = get_screen_size()

    left = cursor_x + gap
    top = cursor_y + gap

    # flip horizontally if the popup would overflow the right edge
    if left + win_w > sw:
        left = cursor_x - win_w - gap
        if left < 0:
            left = gap

    # flip vertically if it would overflow the bottom edge
    if top + win_h > sh:
        top = cursor_y - win_h - gap
        if top < 0:
            top = gap

    return {"left": left, "top": top}


def run_ocr() -> str:
    """Screenshot → tesseract OCR.  Returns recognized text."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    screenshot = CACHE_DIR / "screenshot.png"

    try:
        coords = subprocess.check_output(
            ["slurp", "-d"], text=True
        ).strip()
    except subprocess.CalledProcessError:
        sys.exit(0)          # user cancelled (Escape)

    subprocess.run(["grim", "-g", coords, str(screenshot)], check=True)

    try:
        result = subprocess.check_output(
            ["tesseract", str(screenshot), "-", "-l", "chi_sim+eng"],
            text=True,
        ).strip()
    except subprocess.CalledProcessError:
        result = "[OCR 失败]"

    screenshot.unlink(missing_ok=True)
    return result if result else "（未识别到文字）"


# ── translation (streaming) ──────────────────────────────────────────────────
def translate_stream(text: str, on_token):
    """Run Ollama translation in a daemon thread.

    Calls on_token(full_result) from the main thread via GLib.idle_add
    for every received token, so the UI updates in real time.
    """
    prompt = TRANSLATION_PROMPT.format(text=text)

    def _worker():
        full = ""
        try:
            resp = requests.post(
                OLLAMA_URL,
                json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": True},
                stream=True,
                timeout=120,
            )
            resp.raise_for_status()
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                full += chunk.get("response", "")
                GLib.idle_add(_emit, on_token, full)
                if chunk.get("done"):
                    break
        except Exception as exc:
            GLib.idle_add(_emit, on_token, f"[翻译失败: {exc}]")

    threading.Thread(target=_worker, daemon=True).start()


def _emit(callback, *args):
    """Trampoline for GLib.idle_add — returns False so it only fires once."""
    callback(*args)
    return False


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
        # Clear the previous bounds first. During streaming the new height can
        # exceed the old maximum; setting min first would briefly make
        # min_content_height > max_content_height and trigger a GTK critical.
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
    ):
        super().__init__(application=app, title="翻译")
        self._source_text = source_text
        self._translate = translate

        # ── layer-shell ─────────────────────────────────────────────────
        Gtk4LayerShell.init_for_window(self)
        Gtk4LayerShell.set_layer(self, Gtk4LayerShell.Layer.OVERLAY)
        # position near mouse cursor (fallback to top-right if detection fails)
        try:
            cx, cy = get_cursor_position()
            margins = compute_margins(cx, cy)
        except Exception:
            margins = {"left": None, "top": 20}
            Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.RIGHT, True)
            Gtk4LayerShell.set_margin(self, Gtk4LayerShell.Edge.RIGHT, 20)

        Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.TOP, True)
        Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.LEFT, True)
        Gtk4LayerShell.set_margin(self, Gtk4LayerShell.Edge.TOP, margins["top"])
        Gtk4LayerShell.set_margin(self, Gtk4LayerShell.Edge.LEFT, margins["left"])
        Gtk4LayerShell.set_keyboard_mode(
            self, Gtk4LayerShell.KeyboardMode.EXCLUSIVE
        )

        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_controller)

        self._build_ui()

    def _on_key_pressed(self, _controller, keyval, _keycode, _state):
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    def _build_ui(self):
        # --- CSS ---------------------------------------------------------
        css_path = CONFIG_DIR / "style.css"
        if css_path.exists():
            css_provider = Gtk.CssProvider()
            css_provider.load_from_path(str(css_path))
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(),
                css_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

        # --- root ---------------------------------------------------------
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.set_css_classes(["card"])
        self.set_child(root)

        # --- header -------------------------------------------------------
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

        # --- body (scrollable) --------------------------------------------
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        body.set_css_classes(["body"])
        root.append(body)

        # --- source (always shown) ----------------------------------------
        source_label = Gtk.Label(label="原文")
        source_label.set_css_classes(["section-label"])
        source_label.set_halign(Gtk.Align.START)
        source_label.set_xalign(0)
        body.append(source_label)

        source_text = Gtk.Label(label=self._source_text)
        source_text.set_wrap(True)
        source_text.set_selectable(True)
        source_text.set_xalign(0)
        source_text.set_css_classes(["source-text"])

        source_scroll = Gtk.ScrolledWindow()
        source_scroll.set_child(source_text)
        source_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        body.append(source_scroll)
        sync_scroll_height(source_text, source_scroll, 30, 280)

        # --- translation section (only when --translate) ------------------
        if self._translate:
            sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep.set_css_classes(["separator"])
            body.append(sep)

            trans_label = Gtk.Label(label="译文")
            trans_label.set_css_classes(["section-label"])
            trans_label.set_halign(Gtk.Align.START)
            trans_label.set_xalign(0)
            body.append(trans_label)

            self._translation_label = Gtk.Label(label="翻译中...")
            self._translation_label.set_wrap(True)
            self._translation_label.set_selectable(True)
            self._translation_label.set_xalign(0)
            self._translation_label.set_css_classes(["translation-text"])

            trans_scroll = Gtk.ScrolledWindow()
            trans_scroll.set_child(self._translation_label)
            trans_scroll.set_policy(
                Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
            )
            body.append(trans_scroll)
            self._translation_scroll = trans_scroll
            sync_scroll_height(
                self._translation_label, trans_scroll, 36, 500
            )

        # --- footer -------------------------------------------------------
        footer_box = Gtk.Box()
        footer_box.set_css_classes(["footer"])
        model_name = "Ollama · qwen2.5:3b" if self._translate else "OCR · tesseract"
        footer_label = Gtk.Label(label=model_name)
        footer_label.set_halign(Gtk.Align.END)
        footer_label.set_hexpand(True)
        footer_box.append(footer_label)
        root.append(footer_box)

        # --- start translation (after window is shown) --------------------
        if self._translate:
            GLib.idle_add(self._start_translation)

    def _start_translation(self):
        translate_stream(self._source_text, self._on_token)
        return False  # one-shot idle

    def _on_token(self, full_result: str):
        self._translation_label.set_label(full_result)
        sync_scroll_height(
            self._translation_label, self._translation_scroll, 36, 500
        )


# ── application ──────────────────────────────────────────────────────────────
class TranslateApp(Gtk.Application):
    def __init__(self, source_text: str, translate: bool):
        super().__init__(application_id="com.translate.tool")
        self._source_text = source_text
        self._translate = translate

    def do_activate(self):
        win = TranslateWindow(self, self._source_text, self._translate)
        win.present()
        # auto-close after 15 seconds (longer for translation mode)
        delay = 30 if self._translate else 8
        GLib.timeout_add_seconds(delay, win.close)


# ── cli ──────────────────────────────────────────────────────────────────────
def main():
    # clean up any leftover cache files from previous runs
    if CACHE_DIR.exists():
        for f in CACHE_DIR.iterdir():
            f.unlink(missing_ok=True)

    parser = argparse.ArgumentParser(description="翻译弹窗")
    parser.add_argument("--ocr", action="store_true", help="截图 OCR 识别文字")
    parser.add_argument("--translate", action="store_true", help="调用 Ollama 翻译")
    parser.add_argument("--text", type=str, default=None, help="直接指定文本")
    args = parser.parse_args()

    # --- determine source text -----------------------------------------------
    if args.ocr:
        source_text = run_ocr()
    elif args.text:
        source_text = args.text
    else:
        # demo mode
        source_text = (
            "The quick brown fox jumps over the lazy dog.\n\n"
            "使用方法：\n"
            "  ./run.sh --ocr --translate    截图 → OCR → 翻译\n"
            "  ./run.sh --translate --text …  翻译指定文本\n"
            "  ./run.sh --ocr                 仅 OCR 识别"
        )

    # --- launch window -------------------------------------------------------
    app = TranslateApp(source_text, translate=args.translate)
    return app.run()


if __name__ == "__main__":
    main()
