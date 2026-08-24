"""Command-line entry point.

Kept GTK-free on purpose: ``--help``, dependency checks, capture, and
OCR all run without a display; only the final popup launch imports
``linago.ui``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

from linago import ocr as ocr_mod
from linago.config import (
    load_config,
    load_ocr_settings,
    load_settings,
    warn_secret_permissions,
)
from linago.lang import SOURCE_CHOICES, normalize_text
from linago.paths import cache_dir

REQUIRED_BINS = {
    "capture": ("slurp", "grim"),
    "tesseract": ("tesseract",),
    "position": ("hyprctl",),
}


def check_dependencies(*, need_capture: bool, need_tesseract: bool) -> None:
    """Exit with a clear message when hard dependencies are missing.

    hyprctl is soft: without it the popup falls back to a fixed corner.
    """
    wanted: list[str] = []
    if need_capture:
        wanted += REQUIRED_BINS["capture"]
    if need_tesseract:
        wanted += REQUIRED_BINS["tesseract"]

    seen: set[str] = set()
    missing: list[str] = []
    for binary in wanted:
        if binary not in seen and shutil.which(binary) is None:
            seen.add(binary)
            missing.append(binary)
    if missing:
        print(
            "缺少依赖命令: " + ", ".join(missing) + "\n"
            "请安装对应包后再运行（Arch 示例: grim slurp tesseract "
            "tesseract-data-chi_sim tesseract-data-eng）。",
            file=sys.stderr,
        )
        sys.exit(1)

    if shutil.which("hyprctl") is None:
        print(
            "警告: 未找到 hyprctl，弹窗将显示在屏幕右上角。",
            file=sys.stderr,
        )


def build_parser(provider_names: list[str]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="linago",
        description="OCR + AI 翻译弹窗（Hyprland / Wayland）",
    )
    parser.add_argument("--ocr", action="store_true", help="截图 OCR 识别文字")
    parser.add_argument(
        "--translate", action="store_true", help="调用当前 provider 翻译"
    )
    parser.add_argument("--text", type=str, default=None, help="直接指定文本")
    parser.add_argument(
        "--from",
        dest="from_lang",
        choices=SOURCE_CHOICES,
        default=os.environ.get("TRANSLATE_FROM", "auto"),
        help="源语言（默认 auto：按字符判定）",
    )
    parser.add_argument(
        "--to",
        dest="to_lang",
        choices=SOURCE_CHOICES,
        default=os.environ.get("TRANSLATE_TO", "auto"),
        help="目标语言（默认 auto：取源语言的对面）",
    )
    parser.add_argument(
        "--provider",
        choices=provider_names,
        default=None,
        help=(
            "翻译后端（默认读 settings.toml 的 [app].provider；"
            f"可用: {', '.join(provider_names)}）"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    stale = cache_dir() / "screenshot.png"
    stale.unlink(missing_ok=True)

    settings = load_settings()
    config = load_config(settings)
    ocr_cfg = load_ocr_settings(settings)
    warn_secret_permissions()

    args = build_parser(config.names()).parse_args(argv)

    check_dependencies(
        need_capture=args.ocr,
        need_tesseract=args.ocr and ocr_cfg.engine == "tesseract",
    )

    pending_png = None
    ocr_runner = None
    if args.ocr:
        pending_png = ocr_mod.capture_region(cache_dir())
        source_text = "识别中..."
        langs = ocr_cfg.tesseract_langs

        def ocr_runner(png=pending_png, langs=langs):
            return ocr_mod.run_tesseract(png, langs)

    elif args.text:
        source_text = normalize_text(args.text)
    else:
        active = config.get(args.provider)
        source_text = (
            "The quick brown fox jumps over the lazy dog.\n\n"
            "使用方法：\n"
            "  ./run.sh --ocr --translate\n"
            "  ./run.sh --translate --text …\n"
            "  ./run.sh --translate --provider openai --text …\n\n"
            f"当前后端：{active.display}（{active.type}）\n"
            "配置：settings.toml · 密钥：secrets.toml\n"
            "环境变量：TRANSLATE_PROVIDER / TRANSLATE_MODEL / "
            "TRANSLATE_FROM / TRANSLATE_TO"
        )

    from linago.ui import run_app  # imported late: needs GTK + layer-shell

    return run_app(
        source_text,
        translate=args.translate,
        pending_png=pending_png,
        ocr_runner=ocr_runner,
        from_lang=args.from_lang,
        to_lang=args.to_lang,
        config=config,
        provider_name=args.provider or config.active,
    )


def run() -> None:
    sys.exit(main())
