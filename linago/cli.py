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
import sys

from linago import ocr as ocr_mod
from linago.config import (
    load_actions,
    load_config,
    load_ocr_settings,
    load_settings,
    warn_secret_permissions,
)
from linago.daemon import daemon_alive, default_socket_path, send_request
from linago.i18n import _
from linago.i18n import install as install_i18n
from linago.lang import SOURCE_CHOICES, normalize_text
from linago.ocr import read_primary_selection
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
        "--ocr-multi",
        dest="ocr_multi",
        action="store_true",
        help=_("Capture several regions and OCR them into one text"),
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
    parser.add_argument(
        "--daemon",
        action="store_true",
        help=_(
            "Run resident: keep serving requests on a socket so later "
            "invocations pop up instantly"
        ),
    )
    parser.add_argument(
        "--socket",
        type=str,
        default=None,
        help=_("Daemon socket path (default under XDG_RUNTIME_DIR)"),
    )
    parser.add_argument(
        "--no-forward",
        action="store_true",
        help=_("Never forward to an already-running daemon"),
    )
    parser.add_argument(
        "--history",
        nargs="?",
        const="20",
        default=None,
        metavar="N",
        help=_("Print the last N translations and exit (default 20)"),
    )
    parser.add_argument(
        "--web-port",
        dest="web_port",
        type=int,
        default=8777,
        help=_("Port for the configuration console"),
    )
    parser.add_argument(
        "--no-web",
        dest="no_web",
        action="store_true",
        help=_("Do not start the configuration console with the daemon"),
    )
    parser.add_argument(
        "--web-only",
        dest="web_only",
        action="store_true",
        help=_("Run only the configuration console, without the popup stack"),
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help=_("Run environment and configuration self-checks"),
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help=_("Print machine-readable output where supported"),
    )
    parser.add_argument(
        "--history-clear",
        dest="history_clear",
        action="store_true",
        help=_("Delete the entire local translation history"),
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


def request_from_args(args: argparse.Namespace) -> dict:
    """Serialize an invocation into a daemon socket command."""
    if args.ocr or args.ocr_multi:
        msg: dict = {"cmd": "ocr", "multi": bool(args.ocr_multi)}
    elif args.selection:
        msg = {"cmd": "selection"}
    else:
        msg = {"cmd": "translate", "text": args.text}
    if args.from_lang:
        msg["from"] = args.from_lang
    if args.to_lang:
        msg["to"] = args.to_lang
    if args.provider:
        msg["provider"] = args.provider
    if args.action:
        msg["action"] = args.action
    if args.ocr_engine:
        msg["engine"] = args.ocr_engine
    return msg


def main(argv: list[str] | None = None) -> int:
    stale = cache_dir() / "screenshot.png"
    stale.unlink(missing_ok=True)

    settings = load_settings()
    config = load_config(settings)
    ocr_cfg = load_ocr_settings(settings)
    actions = load_actions(settings)
    compare_names = [
        str(n) for n in ((settings.get("compare") or {}).get("providers") or [])
    ]
    warn_secret_permissions()

    # Language catalog must be installed before argparse help is built.
    configured_lang = (settings.get("app") or {}).get("lang")
    install_i18n(str(configured_lang) if configured_lang else None)

    args = build_parser(config.names()).parse_args(argv)
    setup_logging(verbose=args.verbose)

    socket_path = args.socket or default_socket_path()

    if args.doctor:
        from linago.doctor import has_fatal, run_checks

        checks = run_checks(config)
        if args.as_json:
            import json as _json

            print(
                _json.dumps(
                    [
                        {
                            "name": c.name,
                            "ok": c.ok,
                            "detail": c.detail,
                            "warning": c.warning_only,
                        }
                        for c in checks
                    ],
                    indent=2,
                )
            )
        else:
            for c in checks:
                mark = "ok" if c.ok else ("warn" if c.warning_only else "FAIL")
                print(f"[{mark:>4}] {c.name}: {c.detail}")
        return 1 if has_fatal(checks) else 0

    if args.history_clear:
        from linago.history import History

        removed = History.open_default().clear()
        print(_("Deleted {} entries.").format(removed))
        return 0

    if args.history is not None:
        from datetime import datetime

        from linago.history import History

        entries = History.open_default().recent(max(int(args.history or 20), 1))
        for e in entries:
            stamp = datetime.fromtimestamp(e.ts).strftime("%Y-%m-%d %H:%M")
            route = f"{e.source_lang}->{e.target_lang}"
            backend = e.provider or "-"
            if e.action:
                backend += f"/{e.action}"
            src = e.source_text.replace("\n", " ")[:40]
            dst = e.translated_text.replace("\n", " ")[:40]
            print(f"{stamp}  {route:<5}  {backend}  {src} => {dst}")
        return 0

    # Transparent daemon handoff: when a resident instance answers,
    # forward the request and exit; the popup is shown over there.
    if not args.daemon and not args.no_forward:
        try:
            if daemon_alive(socket_path):
                reply = send_request(socket_path, request_from_args(args))
                if reply.get("ok"):
                    return 0
                print(
                    _("daemon rejected the request: {}").format(reply.get("error", "")),
                    file=sys.stderr,
                )
                return 1
        except OSError:
            logging.getLogger(__name__).debug("daemon probe failed", exc_info=True)

    if args.action and args.action not in actions:
        available = ", ".join(actions) or "（未定义）"
        print(
            _("Unknown action '{}'; defined under [actions]: {}").format(
                args.action, available
            ),
            file=sys.stderr,
        )
        return 2

    if args.action and args.action not in actions:
        available = ", ".join(actions) or _("(none)")
        print(
            _("Unknown action '{}'; defined under [actions]: {}").format(
                args.action, available
            ),
            file=sys.stderr,
        )
        return 2

    default_action = (settings.get("app") or {}).get("action")
    action_name = args.action or (str(default_action) if default_action else None)

    if args.web_only:
        from linago import webserver
        from linago.paths import ensure_config_dir

        context = webserver.ConsoleContext(config_dir=ensure_config_dir())
        try:
            webserver.serve_forever(context, port=args.web_port)
        except KeyboardInterrupt:
            pass
        return 0

    if args.daemon:
        from linago.ui import run_resident  # needs GTK + layer-shell

        return run_resident(
            config=config,
            ocr_settings=ocr_cfg,
            actions=actions,
            action_name=action_name,
            provider_name=args.provider or config.active,
            socket_path=socket_path,
            compare_names=compare_names,
            web_port=args.web_port,
            start_web=not args.no_web,
        )

    capture_requested = args.ocr or args.ocr_multi
    engine = resolve_ocr_engine(args.ocr_engine, ocr_cfg.engine)
    check_dependencies(
        need_capture=capture_requested,
        need_tesseract=capture_requested and engine == "tesseract",
        need_selection=args.selection,
    )

    pending_png = None
    ocr_runner = None
    if capture_requested:
        if args.ocr_multi:
            pending_list = ocr_mod.capture_regions(cache_dir())
        else:
            pending_list = [ocr_mod.capture_region(cache_dir())]
        source_text = _("Recognizing...")
        pending_png = pending_list[0]

        def ocr_runner(_progress=None, pngs=tuple(pending_list), engine=engine):
            return ocr_mod.run_ocr_batch(
                list(pngs), engine=engine, ocr_cfg=ocr_cfg, get_provider=config.get
            )

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
        compare_names=compare_names,
        ocr_engine=engine,
    )


def run() -> None:
    sys.exit(main())
