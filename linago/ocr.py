"""Screen capture (slurp + grim), primary-selection reads, and OCR."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from linago.backends import vision_ocr
from linago.config import OcrSettings
from linago.lang import normalize_text

TESSERACT_LANGS_DEFAULT = "chi_sim+eng"


def capture_region(cache_dir: Path) -> Path:
    """Interactive region select via slurp, then grim capture to PNG.

    Exits silently when the user cancels the selection (Escape).
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    screenshot = cache_dir / "screenshot.png"

    try:
        coords = subprocess.check_output(["slurp", "-d"], text=True).strip()
    except subprocess.CalledProcessError:
        sys.exit(0)

    subprocess.run(["grim", "-g", coords, str(screenshot)], check=True)
    return screenshot


def run_tesseract(png: Path, langs: str = TESSERACT_LANGS_DEFAULT) -> str | None:
    """Run tesseract on an image file.

    Returns the normalized recognized text, "" when nothing was
    recognized, or None when tesseract failed. The image is removed
    either way.
    """
    try:
        result = subprocess.check_output(
            ["tesseract", str(png), "-", "-l", langs],
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, OSError):
        png.unlink(missing_ok=True)
        return None

    png.unlink(missing_ok=True)
    return normalize_text(result)


def forward_to_translation(text: str | None) -> bool:
    """Whether OCR output may be sent to the translator.

    Failures (None) and empty captures must never reach the provider;
    translating an error placeholder wastes a request and produces
    noise. The popup still shows what happened in the source pane.
    """
    return bool(text and text.strip())


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


def make_ocr_runner(
    png: Path,
    *,
    engine: str,
    ocr_cfg: OcrSettings,
    get_provider: Callable[[str | None], object],
) -> Callable[[], str | None]:
    """Build the zero-arg OCR callable for a captured screenshot.

    ``get_provider`` resolves the vision provider by name (None meaning
    the active one); both engine branches return str | None with the
    failure contract of run_tesseract/vision_ocr.
    """
    if engine == "vision":
        provider = get_provider(ocr_cfg.provider)

        def vision_runner():
            return vision_ocr(provider, png)

        return vision_runner

    langs = ocr_cfg.tesseract_langs

    def tesseract_runner():
        return run_tesseract(png, langs)

    return tesseract_runner
