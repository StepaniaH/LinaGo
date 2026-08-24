"""GTK4 layer-shell popup UI.

This module needs GTK 4 and gtk4-layer-shell at import time; the CLI
imports it lazily so ``--help``, dependency checks, and tests work on
machines without a Wayland stack.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, GLib, Gtk, Gtk4LayerShell  # noqa: E402

from linago.backends import stream_completion, tts_speech  # noqa: E402
from linago.config import AppConfig, OcrSettings  # noqa: E402
from linago.history import Entry, History  # noqa: E402
from linago.i18n import _  # noqa: E402
from linago.lang import (  # noqa: E402
    LANGUAGES,
    SOURCE_CHOICES,
    TARGET_CHOICES,
    LangPair,
    build_prompt,
    choice_label,
    normalize_text,
    opposite_lang,
    resolve_pair,
)
from linago.ocr import (  # noqa: E402
    capture_region,
    forward_to_translation,
    make_ocr_runner,
    read_primary_selection,
)
from linago.paths import cache_dir  # noqa: E402
from linago.placement import (  # noqa: E402
    BODY_PAD_V,
    active_monitor,
    compute_placement,
    compute_section_caps,
    get_cursor_position,
)
from linago.playback import play_file  # noqa: E402

SOURCE_EDIT_DEBOUNCE_MS = 700  # wait for typing to settle before retranslating


def _emit(callback, *args):
    """Trampoline for GLib.idle_add — returns False so it only fires once."""
    callback(*args)
    return False


# ── translation (streaming) ──────────────────────────────────────────────────
def translate_stream(
    provider,
    text: str,
    pair: LangPair,
    on_token,
    cancel: threading.Event,
    template: str | None = None,
    on_done=None,
):
    """Run translation via the active provider in a daemon thread.

    ``on_done(final_text)`` fires once on the UI thread after an
    uncancelled stream produced output.
    """
    prompt = build_prompt(text, pair, template)

    def _worker():
        latest = {"full": ""}

        def _ui_token(full: str):
            latest["full"] = full
            GLib.idle_add(_emit, on_token, normalize_text(full))

        try:
            stream_completion(provider, prompt, _ui_token, cancel)
        except Exception as exc:
            logging.getLogger(__name__).error("translation failed: %s", exc)
            if not cancel.is_set():
                GLib.idle_add(
                    _emit,
                    on_token,
                    _("Translation failed: {}").format(exc),
                )
            return

        final = normalize_text(latest["full"])
        if not cancel.is_set() and final and on_done is not None:
            GLib.idle_add(_emit, on_done, final)

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
        self.copy_btn.set_tooltip_text(_("Copy"))
        self.copy_btn.set_halign(Gtk.Align.END)
        self.copy_btn.set_valign(Gtk.Align.END)
        self.copy_btn.set_can_focus(False)
        self.copy_btn.connect("clicked", self._on_copy_clicked)

        overlay = Gtk.Overlay()
        overlay.set_child(self.scroll)
        overlay.add_overlay(self.copy_btn)
        body.append(overlay)
        # exposed so callers can add pane-specific controls (TTS, …)
        self.overlay = overlay

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

    def update_max_h(self, max_h: int):
        """Adopt a recomputed height cap and re-fit the scroller."""
        if max_h == self.max_h:
            return
        self.max_h = max_h
        self.sync_height()

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
        pending_png: Path | None = None,
        ocr_runner: Callable[[], str | None] | None = None,
        from_lang: str = "auto",
        to_lang: str = "auto",
        actions: dict[str, str] | None = None,
        action_name: str | None = None,
        config: AppConfig | None = None,
        provider_name: str | None = None,
        completion_cb: Callable[[dict], None] | None = None,
        history: History | None = None,
        tts_provider=None,
    ):
        super().__init__(application=app, title=_("Translate"))
        self._source_text = source_text
        self._translate = translate
        self._pending_png = pending_png
        self._ocr_runner = ocr_runner
        self._from_lang = from_lang
        self._to_lang = to_lang
        self._actions = dict(actions or {})
        self._action_name = action_name if action_name in self._actions else None
        self._config = config or load_app_config()
        self._provider_name = provider_name or self._config.active
        self._completion_cb = completion_cb
        self._history = history
        self._tts = tts_provider
        self._speaking = False
        self._pinned = False
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
        self._action_dropdown: Gtk.DropDown | None = None
        self._footer_label: Gtk.Label | None = None
        self._source_max_h = 280
        self._translation_max_h = 500

        self._setup_layer_shell()

        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_controller)
        self.connect("close-request", self._on_close_request)

        self._build_ui()

    def _provider(self):
        return self._config.get(self._provider_name)

    def _setup_layer_shell(self):
        Gtk4LayerShell.init_for_window(self)
        Gtk4LayerShell.set_layer(self, Gtk4LayerShell.Layer.OVERLAY)
        Gtk4LayerShell.set_keyboard_mode(self, Gtk4LayerShell.KeyboardMode.EXCLUSIVE)

        self._avail_h = 600  # refined below or by measured chrome
        try:
            pos = get_cursor_position()
            if pos is None:
                raise RuntimeError("cursor position unavailable")
            placement = compute_placement(pos[0], pos[1], active_monitor())
            self._avail_h = placement.avail_h

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
                Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.BOTTOM, True)
                Gtk4LayerShell.set_margin(
                    self, Gtk4LayerShell.Edge.BOTTOM, placement.bottom_margin
                )

            self._source_max_h, self._translation_max_h = compute_section_caps(
                self._avail_h, self._translate
            )
        except Exception:
            Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.TOP, True)
            Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.RIGHT, True)
            Gtk4LayerShell.set_margin(self, Gtk4LayerShell.Edge.TOP, 20)
            Gtk4LayerShell.set_margin(self, Gtk4LayerShell.Edge.RIGHT, 20)

    def _add_speak_button(self):
        btn = Gtk.Button(label="🔊")
        btn.set_css_classes(["copy-btn", "speak-btn"])
        btn.set_halign(Gtk.Align.START)
        btn.set_valign(Gtk.Align.END)
        btn.set_can_focus(False)
        btn.set_tooltip_text(_("Speak the translation"))
        btn.connect("clicked", self._on_speak_clicked)
        self._translation_section.overlay.add_overlay(btn)

    def _on_speak_clicked(self, btn):
        if self._speaking or not self._translation_section:
            return
        text = self._translation_section.get_text().strip()
        if not text or text == "—" or text == _("Translating..."):
            return
        self._speaking = True
        btn.set_label("…")
        provider = self._tts

        def _worker():
            try:
                audio = tts_speech(provider, text)
                out = cache_dir() / f"tts-{os.getpid()}.mp3"
                cache_dir().mkdir(parents=True, exist_ok=True)
                out.write_bytes(audio)
                play_file(out)
            except Exception:
                logging.getLogger(__name__).exception("speech failed")

            def _reset():
                btn.set_label("🔊")
                self._speaking = False
                return False

            GLib.idle_add(_reset)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_pin_clicked(self, btn):
        self._pinned = not self._pinned
        tooltip = (
            _("Unpin (Esc closes again)")
            if self._pinned
            else _("Pin: keep the card open (Esc ignored)")
        )
        btn.set_tooltip_text(tooltip)
        css = ["pin-btn"]
        if self._pinned:
            css.append("pinned")
        btn.set_css_classes(css)

    def _on_key_pressed(self, _controller, keyval, _keycode, _state):
        if keyval == Gdk.KEY_Escape:
            if self._pinned:
                return True  # pinned cards ignore Esc; ✕ still closes
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
        css_path = _find_style_css()
        if css_path is not None:
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
        self._header_box = header

        title = Gtk.Label(label=_("Translate"))
        title.set_css_classes(["title"])
        title.set_halign(Gtk.Align.START)
        title.set_hexpand(True)

        pin_btn = Gtk.Button(label="📌")
        pin_btn.set_css_classes(["pin-btn"])
        pin_btn.set_tooltip_text(_("Pin: keep the card open (Esc ignored)"))
        pin_btn.set_can_focus(False)
        pin_btn.connect("clicked", self._on_pin_clicked)

        close_btn = Gtk.Button(label="✕")
        close_btn.set_css_classes(["close-btn"])
        close_btn.connect("clicked", lambda _b: self.close())

        header.append(title)
        header.append(pin_btn)
        header.append(close_btn)
        root.append(header)

        self._lang_bar: Gtk.Box | None = None
        if self._translate:
            self._lang_bar = self._build_lang_bar()
            root.append(self._lang_bar)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        body.set_css_classes(["body"])
        root.append(body)

        self._source_section = TextSection(
            body,
            section=_("Source"),
            text=self._source_text,
            css_class="source-text",
            min_h=30,
            max_h=self._source_max_h,
            editable=True,
        )
        if self._source_section.buffer is not None:
            self._source_section.buffer.connect("changed", self._on_source_changed)
        source_keys = Gtk.EventControllerKey()
        source_keys.connect("key-pressed", self._on_source_key_pressed)
        self._source_section.view.add_controller(source_keys)

        self._separator: Gtk.Separator | None = None
        if self._translate:
            sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep.set_css_classes(["separator"])
            body.append(sep)
            self._separator = sep

            waiting = (
                _("Waiting for OCR...") if self._pending_png else _("Translating...")
            )
            self._translation_section = TextSection(
                body,
                section=_("Translation"),
                text=waiting,
                css_class="translation-text",
                min_h=36,
                max_h=self._translation_max_h,
            )
            if self._tts is not None:
                self._add_speak_button()
            self._refresh_pair_labels()

        footer_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer_box.set_css_classes(["footer"])
        self._footer_box = footer_box
        if self._translate and self._actions:
            entries = [_("Translate"), *self._actions.keys()]
            store = Gtk.StringList.new(entries)
            self._action_dropdown = Gtk.DropDown.new(store, None)
            selected = 0
            if self._action_name is not None:
                try:
                    selected = list(self._actions).index(self._action_name) + 1
                except ValueError:
                    selected = 0
                    self._action_name = None
            self._action_dropdown.set_selected(selected)
            self._action_dropdown.set_css_classes(["action-dropdown"])
            self._action_dropdown.set_tooltip_text(
                _("Action to apply to the source text")
            )
            self._action_dropdown.connect("notify::selected", self._on_action_changed)
            footer_box.append(self._action_dropdown)
        if self._translate:
            names = self._config.names()
            labels = [self._config.get(n).display for n in names]
            store = Gtk.StringList.new(labels)
            self._provider_dropdown = Gtk.DropDown.new(store, None)
            try:
                self._provider_dropdown.set_selected(names.index(self._provider_name))
            except ValueError:
                self._provider_dropdown.set_selected(0)
                self._provider_name = names[0]
            self._provider_dropdown.set_css_classes(["provider-dropdown"])
            self._provider_dropdown.set_tooltip_text(
                _("Switch translation backend (local Ollama / BYOK)")
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

        GLib.idle_add(self._apply_measured_chrome)

        if self._pending_png:
            GLib.idle_add(self._start_ocr)
        elif self._translate:
            GLib.idle_add(self._start_translation)

    def _apply_measured_chrome(self):
        """Replace estimated chrome heights with real widget measures.

        The pre-layout caps rely on rough constants; once the widgets
        exist we can measure them and redistribute the vertical budget
        so theme/font changes don't push the card off-screen.
        """
        if self._closed:
            return False

        def nat(widget: Gtk.Widget | None) -> int:
            if widget is None:
                return 0
            _mn, natural, _mb, _nb = widget.measure(Gtk.Orientation.VERTICAL, -1)
            return natural

        chrome = BODY_PAD_V
        chrome += nat(getattr(self, "_header_box", None))
        chrome += nat(self._lang_bar)
        chrome += nat(self._separator)
        chrome += nat(self._footer_box)
        if self._source_section:
            chrome += nat(self._source_section.section_label)
        if self._translation_section:
            chrome += nat(self._translation_section.section_label)

        source_cap, translation_cap = compute_section_caps(
            self._avail_h, self._translate, chrome_h=chrome
        )
        self._source_max_h = source_cap
        self._translation_max_h = translation_cap
        if self._source_section:
            self._source_section.update_max_h(source_cap)
        if self._translation_section:
            self._translation_section.update_max_h(translation_cap)
        return False

    def _build_lang_bar(self) -> Gtk.Box:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.set_css_classes(["lang-bar"])

        self._from_dropdown = _make_lang_dropdown(SOURCE_CHOICES, self._from_lang)
        self._to_dropdown = _make_lang_dropdown(TARGET_CHOICES, self._to_lang)

        swap_btn = Gtk.Button(label="⇄")
        swap_btn.set_css_classes(["swap-btn"])
        swap_btn.set_tooltip_text(_("Swap languages"))
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
                    _("Source · {} (auto)").format(src_name)
                )
            else:
                self._source_section.set_section_label(
                    _("Source · {}").format(src_name)
                )
        if self._translation_section:
            tgt_name = LANGUAGES[pair.target]["label"]
            self._translation_section.set_section_label(
                _("Translation · {}").format(tgt_name)
            )
        # Refresh "自动 · 英/中" labels on dropdowns without firing handlers
        self._updating_lang_ui = True
        try:
            for dropdown, choices, detected_for in (
                (self._from_dropdown, SOURCE_CHOICES, pair.detected),
                (
                    self._to_dropdown,
                    TARGET_CHOICES,
                    opposite_lang(pair.source),
                ),
            ):
                if dropdown is None:
                    continue
                model = dropdown.get_model()
                if isinstance(model, Gtk.StringList):
                    for i, code in enumerate(choices):
                        det = detected_for if code == "auto" else None
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
        new_from = pair.target if self._from_lang == "auto" else self._to_lang
        new_to = pair.source if self._to_lang == "auto" else self._from_lang
        if self._from_lang != "auto" and self._to_lang != "auto":
            new_from, new_to = self._to_lang, self._from_lang

        self._from_lang = new_from if new_from != "auto" else pair.target
        self._to_lang = new_to if new_to != "auto" else pair.source

        self._updating_lang_ui = True
        try:
            if self._from_dropdown is not None:
                self._from_dropdown.set_selected(SOURCE_CHOICES.index(self._from_lang))
            if self._to_dropdown is not None:
                self._to_dropdown.set_selected(TARGET_CHOICES.index(self._to_lang))
        finally:
            self._updating_lang_ui = False

        self._on_lang_pair_changed()

    def _on_lang_pair_changed(self):
        self._refresh_pair_labels()
        if self._pending_png:
            return  # wait until OCR finishes
        if self._translate and not self._is_placeholder_source():
            self._restart_translation()

    def _refresh_provider_footer(self):
        if not self._footer_label:
            return
        p = self._provider()
        kind = _("Local") if p.type == "ollama" else "BYOK"
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
        if self._pending_png:
            return
        if self._translate and not self._is_placeholder_source():
            self._restart_translation()

    def _is_placeholder_source(self) -> bool:
        return self._source_text in (_("Recognizing..."), _("Waiting for OCR..."))

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
        self._pending_png = None
        self._refresh_pair_labels()
        if self._translate:
            self._restart_translation()
        return False

    def _start_ocr(self):
        png = self._pending_png
        assert png is not None

        def _worker():
            try:
                text = self._ocr_runner() if self._ocr_runner else ""
            except Exception:
                text = None  # surfaced below as an OCR failure
            if not self._closed:
                GLib.idle_add(_emit, self._on_ocr_done, text)

        threading.Thread(target=_worker, daemon=True).start()
        return False

    def _on_ocr_done(self, text):
        if self._closed:
            return
        usable = forward_to_translation(text)
        if text is None:
            display = _("OCR failed")
        elif not text:
            display = _("No text recognized")
        else:
            display = text
        self._source_text = display
        self._pending_png = None
        if self._source_section:
            self._updating_source_ui = True
            try:
                self._source_section.set_text(display)
            finally:
                self._updating_source_ui = False
        self._refresh_pair_labels()
        if not self._translate:
            return
        if usable:
            self._start_translation()
        elif self._translation_section:
            self._translation_section.set_text("—")

    def _restart_translation(self):
        self._cancel.set()
        self._cancel = threading.Event()
        if self._translation_section:
            self._translation_section.set_text(_("Translating..."))
        self._start_translation()

    def _current_template(self) -> str | None:
        """Prompt template of the selected action; None = plain translate."""
        return self._actions.get(self._action_name or "")

    def _on_action_changed(self, dropdown, _pspec):
        names = list(self._actions)
        idx = dropdown.get_selected()
        new = None if idx <= 0 else names[idx - 1]
        if new == self._action_name:
            return
        self._action_name = new
        if self._pending_png:
            return
        if self._translate and not self._is_placeholder_source():
            self._restart_translation()

    def _start_translation(self):
        if self._closed or self._is_placeholder_source():
            return False
        self._gen += 1
        gen = self._gen
        pair = self._current_pair()
        provider = self._provider()
        template = self._current_template()
        action = self._action_name
        self._refresh_pair_labels()

        def _on_token(full_result: str):
            if gen != self._gen:
                return
            self._on_token(full_result)

        def _on_done(final_text: str):
            if gen != self._gen:
                return
            if self._completion_cb is not None:
                try:
                    self._completion_cb(
                        {
                            "event": "translation",
                            "source": self._source_text,
                            "translated": final_text,
                            "source_lang": pair.source,
                            "target_lang": pair.target,
                            "provider": provider.name,
                            "action": action,
                        }
                    )
                except Exception:
                    logging.getLogger(__name__).exception("completion callback failed")
            if self._history is not None:
                try:
                    self._history.add(
                        Entry(
                            ts=time.time(),
                            source_lang=pair.source,
                            target_lang=pair.target,
                            source_text=self._source_text,
                            translated_text=final_text,
                            provider=provider.name,
                            action=action,
                        )
                    )
                except Exception:
                    logging.getLogger(__name__).exception("history write failed")

        translate_stream(
            provider,
            self._source_text,
            pair,
            _on_token,
            self._cancel,
            template=template,
            on_done=_on_done,
        )
        return False

    def _on_token(self, full_result: str):
        if self._closed or not self._translation_section:
            return
        self._translation_section.set_text(full_result)


def load_app_config() -> AppConfig:
    from linago.config import load_config, load_settings

    return load_config(load_settings())


def _find_style_css() -> Path | None:
    """style.css lives beside settings.toml when a config dir exists."""
    from linago.paths import find_config_dir

    config_dir = find_config_dir()
    if config_dir is None:
        return None
    path = config_dir / "style.css"
    return path if path.exists() else None


# ── application ──────────────────────────────────────────────────────────────
class TranslateApp(Gtk.Application):
    def __init__(
        self,
        source_text: str,
        translate: bool,
        pending_png: Path | None = None,
        ocr_runner: Callable[[], str | None] | None = None,
        from_lang: str = "auto",
        to_lang: str = "auto",
        actions: dict[str, str] | None = None,
        action_name: str | None = None,
        config: AppConfig | None = None,
        provider_name: str | None = None,
        ocr_settings: OcrSettings | None = None,
        resident: bool = False,
        history: History | None = None,
        tts_provider=None,
    ):
        super().__init__(application_id="io.github.stepaniah.linago")
        self._source_text = source_text
        self._translate = translate
        self._pending_png = pending_png
        self._ocr_runner = ocr_runner
        self._from_lang = from_lang
        self._to_lang = to_lang
        self._actions = actions or {}
        self._action_name = action_name
        self._config = config or load_app_config()
        self._provider_name = provider_name or self._config.active
        self._ocr_settings = ocr_settings
        self._resident = resident
        self._history = history
        self._tts_provider = tts_provider
        self._window: TranslateWindow | None = None
        self.event_publisher = None  # set by run_resident()

    def do_activate(self):
        if self._resident:
            # Keep the main loop alive between popups.
            self.hold()
            return
        self._open_window(
            source_text=self._source_text,
            translate=self._translate,
            pending_png=self._pending_png,
            ocr_runner=self._ocr_runner,
            from_lang=self._from_lang,
            to_lang=self._to_lang,
            action_name=self._action_name,
        )

    def _open_window(
        self,
        *,
        source_text,
        translate,
        pending_png=None,
        ocr_runner=None,
        from_lang="auto",
        to_lang="auto",
        action_name=None,
    ):
        if self._window is not None and not self._window._closed:
            self._window.close()
        self._window = TranslateWindow(
            self,
            source_text,
            translate,
            pending_png=pending_png,
            ocr_runner=ocr_runner,
            from_lang=from_lang,
            to_lang=to_lang,
            actions=self._actions,
            action_name=action_name,
            config=self._config,
            provider_name=self._provider_name,
            completion_cb=(self.event_publisher if self.event_publisher else None),
            history=self._history,
            tts_provider=self._tts_provider,
        )
        self._window.present()

    def present_payload(self, payload: dict) -> bool:
        """Open a popup for a daemon request (runs on the UI thread)."""
        kind = payload.get("kind")
        engine = self._ocr_settings.engine if self._ocr_settings else "tesseract"
        engine = payload.get("engine") or engine
        if kind == "translate":
            text = payload.get("text")
            if text is None:
                return False
            self._open_window(
                source_text=normalize_text(text),
                translate=True,
            )
        elif kind == "selection":
            selected = read_primary_selection()
            if not selected or not selected.strip():
                return False
            self._open_window(
                source_text=normalize_text(selected),
                translate=True,
            )
        elif kind == "ocr":
            png = capture_region(cache_dir())
            runner = make_ocr_runner(
                png,
                engine=engine,
                ocr_cfg=self._ocr_settings or OcrSettings(),
                get_provider=self._config.get,
            )
            self._open_window(
                source_text=_("Recognizing..."),
                translate=True,
                pending_png=png,
                ocr_runner=runner,
            )
        return False


def run_app(
    source_text: str,
    *,
    translate: bool,
    pending_png: Path | None = None,
    ocr_runner: Callable[[], str | None] | None = None,
    from_lang: str = "auto",
    to_lang: str = "auto",
    actions: dict[str, str] | None = None,
    action_name: str | None = None,
    config: AppConfig | None = None,
    provider_name: str | None = None,
) -> int:
    """Create the popup application and run its main loop."""
    app = TranslateApp(
        source_text,
        translate,
        history=_load_history(),
        tts_provider=_resolve_tts(config),
        pending_png=pending_png,
        ocr_runner=ocr_runner,
        from_lang=from_lang,
        to_lang=to_lang,
        actions=actions,
        action_name=action_name,
        config=config,
        provider_name=provider_name,
    )
    return app.run(None)


def run_resident(
    *,
    config: AppConfig,
    ocr_settings: OcrSettings,
    actions: dict[str, str],
    action_name: str | None = None,
    provider_name: str | None = None,
    socket_path: str,
) -> int:
    """Serve socket requests until the process is terminated."""
    import sys as _sys

    from linago import daemon

    app = TranslateApp(
        "",
        translate=False,
        actions=actions,
        tts_provider=_resolve_tts(config),
        history=_load_history(),
        action_name=action_name,
        config=config,
        provider_name=provider_name,
        ocr_settings=ocr_settings,
        resident=True,
    )

    server = daemon.Server(socket_path, on_request=app.present_payload)
    app.event_publisher = server.events.publish

    try:
        server.start()
    except RuntimeError as exc:
        print(str(exc), file=_sys.stderr)
        return 1

    try:
        code = app.run(None)
    finally:
        server.stop()
        try:
            os.unlink(socket_path)
        except OSError:
            pass
    return code


def _load_history() -> History | None:
    """Open the local history store unless disabled via [history]."""
    from linago.config import load_settings

    settings = load_settings()
    if not (settings.get("history") or {}).get("enabled", True):
        return None
    try:
        return History.open_default()
    except Exception:
        logging.getLogger(__name__).warning("history unavailable", exc_info=True)
        return None


def _resolve_tts(config: AppConfig):
    """Provider for [tts] speech synthesis; None disables the control."""
    from linago.config import load_settings, load_tts_provider

    name = load_tts_provider(load_settings())
    if not name:
        return None
    try:
        return config.get(name)
    except KeyError:
        logging.getLogger(__name__).warning(
            "tts provider '%s' not defined; disabling speech", name
        )
        return None
