"""Screen capture (slurp + grim), primary-selection reads, and OCR."""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from linago.backends import stream_vision_ocr
from linago.config import OcrSettings
from linago.lang import normalize_text

logger = logging.getLogger(__name__)

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
) -> Callable[[Callable | None], str | None]:
    """Build the OCR callable for a captured screenshot.

    The callable takes an optional ``progress(full_so_far)`` sink: the
    vision engine streams transcription updates into it, tesseract
    ignores it. Return values follow the failure contract of
    run_tesseract/vision_ocr.
    """
    if engine == "vision":
        provider = get_provider(ocr_cfg.provider)

        def vision_runner(progress=None):
            return stream_vision_ocr(provider, png, progress, None)

        return vision_runner

    langs = ocr_cfg.tesseract_langs

    def tesseract_runner(_progress=None):
        return run_tesseract(png, langs)

    return tesseract_runner


def capture_regions(cache_dir: Path, *, max_regions: int = 8) -> list[Path]:
    """Interactive repeated region selection; Escape stops collecting.

    The first Escape cancels outright (empty result), matching the
    single-region flow where cancelling exits silently.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index in range(max_regions):
        try:
            coords = subprocess.check_output(["slurp", "-d"], text=True).strip()
        except subprocess.CalledProcessError:
            break
        target = cache_dir / f"region-{index}.png"
        subprocess.run(["grim", "-g", coords, str(target)], check=True)
        paths.append(target)
    return paths


def run_ocr_batch(
    pngs: list[Path],
    *,
    engine: str,
    ocr_cfg: OcrSettings,
    get_provider,
) -> str:
    """OCR several screenshots and join recognized blocks with blank lines.

    Returns "" when nothing was recognized anywhere; images are removed
    immediately after their own pass.
    """
    parts: list[str] = []
    for png in pngs:
        try:
            if engine == "vision":
                provider = get_provider(ocr_cfg.provider)
                text = stream_vision_ocr(provider, png, None, None)
            else:
                text = run_tesseract(png, ocr_cfg.tesseract_langs)
        except Exception:
            logger.warning("batch OCR failed for %s", png.name, exc_info=True)
            text = None
        finally:
            png.unlink(missing_ok=True)
        if text:
            parts.append(text)
    return "\n\n".join(parts)
