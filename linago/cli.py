"""Command-line entry point.

Kept GTK-free on purpose: ``--help``, dependency checks, capture, and
OCR all run without a display; only the final popup launch imports
``linago.ui``.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys

from linago import ocr as ocr_mod
from linago.backends import vision_ocr
from linago.config import (
    load_actions,
    load_config,
    load_ocr_settings,
    load_settings,
    warn_secret_permissions,
)
from linago.i18n import _
from linago.i18n import install as install_i18n
from linago.lang import SOURCE_CHOICES, normalize_text
from linago.paths import cache_dir

REQUIRED_BINS = {
    "capture": ("slurp", "grim"),
    "tesseract": ("tesseract",),
    "selection": ("wl-paste",),
    "position": ("hyprctl",),
}


def check_dependencies(
    *,
    need_capture: bool = False,
    need_tesseract: bool = False,
    need_selection: bool = False,
) -> None:
    """Exit with a clear message when hard dependencies are missing.

    hyprctl is soft: without it the popup falls back to a fixed corner.
    """
    wanted: list[str] = []
    if need_capture:
        wanted += REQUIRED_BINS["capture"]
    if need_tesseract:
        wanted += REQUIRED_BINS["tesseract"]
    if need_selection:
        wanted += REQUIRED_BINS["selection"]

    seen: set[str] = set()
    missing: list[str] = []
    for binary in wanted:
        if binary not in seen and shutil.which(binary) is None:
            seen.add(binary)
            missing.append(binary)
    if missing:
        print(
            _("Missing required commands: {}").format(", ".join(missing))
            + "\n"
            + _(
                "Install them first (Arch example: grim slurp tesseract "
                "tesseract-data-chi_sim tesseract-data-eng)."
            ),
            file=sys.stderr,
        )
        sys.exit(1)

    if shutil.which("hyprctl") is None:
        print(
            _(
                "Warning: hyprctl not found; the popup will show "
                "in the top-right corner."
            ),
            file=sys.stderr,
        )


def build_parser(provider_names: list[str]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="linago",
        description=_("OCR + AI translation popup for Hyprland / Wayland"),
    )
    parser.add_argument(
        "--ocr",
        action="store_true",
        help=_("Capture a screen region and OCR it"),
    )
    parser.add_argument(
        "--ocr-engine",
        dest="ocr_engine",
        choices=OCR_ENGINES,
        default=os.environ.get("TRANSLATE_OCR_ENGINE"),
        help=_("OCR engine (default from [ocr].engine in settings.toml)"),
    )
    parser.add_argument(
        "--selection",
        action="store_true",
        help=_("Translate the primary selection (needs wl-clipboard)"),
    )
    parser.add_argument(
        "--translate",
        action="store_true",
        help=_("Translate using the active provider"),
    )
    parser.add_argument(
        "--text",
        type=str,
        default=None,
        help=_("Use this text instead of capturing"),
    )
    parser.add_argument(
        "--from",
        dest="from_lang",
        choices=SOURCE_CHOICES,
        default=os.environ.get("TRANSLATE_FROM", "auto"),
        help=_("Source language (default auto: detect from script)"),
    )
    parser.add_argument(
        "--to",
        dest="to_lang",
        choices=SOURCE_CHOICES,
        default=os.environ.get("TRANSLATE_TO", "auto"),
        help=_("Target language (default auto: peer of the source)"),
    )
    parser.add_argument(
        "--provider",
        choices=provider_names,
        default=None,
        help=(
            _(
                "Translation backend (default from [app].provider; available: {})"
            ).format(", ".join(provider_names))
        ),
    )
    parser.add_argument(
        "--action",
        dest="action",
        default=os.environ.get("TRANSLATE_ACTION"),
        help=_("Run an action defined under [actions] instead of translating"),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help=_("Verbose logging (also written to linago.log in the cache dir)"),
    )
    return parser


OCR_ENGINES = ("tesseract", "vision")


def resolve_ocr_engine(flag: str | None, configured: str) -> str:
    """CLI flag > env > settings; unknown values fall back to tesseract."""
    engine = flag or configured
    return engine if engine in OCR_ENGINES and engine == "vision" else "tesseract"


def setup_logging(*, verbose: bool) -> None:
    """WARNING→stderr by default; DEBUG also appends to the cache log."""
    logger = logging.getLogger("linago")
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    if verbose:
        cache_dir().mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(cache_dir() / "linago.log")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
        logger.setLevel(logging.DEBUG)
    else:
        logger.addHandler(logging.StreamHandler())
        logger.setLevel(logging.WARNING)


def read_primary_selection() -> str | None:
    """Primary-selection text via wl-paste; None when unavailable/empty.

    wl-clipboard exits non-zero when the selection is empty, which is
    reported the same way as a missing tool: nothing to translate.
    """
    try:
        proc = subprocess.run(
            ["wl-paste", "--primary", "--no-newline"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout or None


def main(argv: list[str] | None = None) -> int:
    stale = cache_dir() / "screenshot.png"
    stale.unlink(missing_ok=True)

    settings = load_settings()
    config = load_config(settings)
    ocr_cfg = load_ocr_settings(settings)
    actions = load_actions(settings)
    warn_secret_permissions()

    # Language catalog must be installed before argparse help is built.
    configured_lang = (settings.get("app") or {}).get("lang")
    install_i18n(str(configured_lang) if configured_lang else None)

    args = build_parser(config.names()).parse_args(argv)
    setup_logging(verbose=args.verbose)

    if args.action and args.action not in actions:
        available = ", ".join(actions) or "（未定义）"
        print(
            _("Unknown action '{}'; defined under [actions]: {}").format(
                args.action, available
            ),
            file=sys.stderr,
        )
        return 2

    engine = resolve_ocr_engine(args.ocr_engine, ocr_cfg.engine)
    check_dependencies(
        need_capture=args.ocr,
        need_tesseract=args.ocr and engine == "tesseract",
        need_selection=args.selection,
    )

    pending_png = None
    ocr_runner = None
    if args.ocr:
        pending_png = ocr_mod.capture_region(cache_dir())
        source_text = _("Recognizing...")
        if engine == "vision":
            vision_provider = (
                config.get(ocr_cfg.provider) if ocr_cfg.provider else config.get()
            )

            def ocr_runner(png=pending_png, vp=vision_provider):
                return vision_ocr(vp, png)

        else:
            langs = ocr_cfg.tesseract_langs

            def ocr_runner(png=pending_png, langs=langs):
                return ocr_mod.run_tesseract(png, langs)

    elif args.selection:
        selected = read_primary_selection()
        if not selected or not selected.strip():
            print(_("Primary selection is empty or unavailable."), file=sys.stderr)
            return 1
        source_text = normalize_text(selected)
    elif args.text:
        source_text = normalize_text(args.text)
    else:
        active = config.get(args.provider)
        source_text = (
            "The quick brown fox jumps over the lazy dog.\n\n"
            + _("Usage:")
            + "\n"
            + "  ./run.sh --ocr --translate\n"
            + "  ./run.sh --translate --text …\n"
            + "  ./run.sh --translate --provider openai --text …\n\n"
            + _("Active backend: {} ({})").format(active.display, active.type)
            + "\n"
            + _("Config: settings.toml · Keys: secrets.toml")
            + "\n"
            + _(
                "Env overrides: TRANSLATE_PROVIDER / TRANSLATE_MODEL / "
                "TRANSLATE_FROM / TRANSLATE_TO"
            )
        )

    from linago.ui import run_app  # imported late: needs GTK + layer-shell

    default_action = (settings.get("app") or {}).get("action")
    action_name = args.action or (str(default_action) if default_action else None)

    return run_app(
        source_text,
        translate=args.translate,
        pending_png=pending_png,
        ocr_runner=ocr_runner,
        from_lang=args.from_lang,
        to_lang=args.to_lang,
        actions=actions,
        action_name=action_name,
        config=config,
        provider_name=args.provider or config.active,
    )


def run() -> None:
    sys.exit(main())
