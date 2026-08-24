"""Language table, detection heuristics, and prompt construction.

The language table drives CLI choices, dropdown labels, and prompts.
``auto`` is virtual: it resolves from the text itself. Detection is
script-based, so it can distinguish English / Chinese / Japanese /
Korean / Russian but cannot separate languages sharing the Latin
script (French, German, Spanish) — those must be selected explicitly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

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

_HAN_RE = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff"
    r"\U00020000-\U0002a6df]"
)
_LATIN_RE = re.compile(r"[A-Za-z]")
_BLANK_LINES_RE = re.compile(r"\n[ \t]*\n+")


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


def detect_lang(text: str) -> str:
    """Heuristic language detection from Unicode script mix.

    Returns one of the keys of :data:`LANGUAGES`. Han characters map to
    ``zh``; anything else defaults to ``en``.
    """
    han = len(_HAN_RE.findall(text))
    latin = len(_LATIN_RE.findall(text))
    if han == 0 and latin == 0:
        return "en"
    return "zh" if han > latin else "en"


def opposite_lang(code: str) -> str:
    """The default translation peer of a language."""
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
    """Dropdown label for a choice code; ``auto`` may show detection."""
    if code == "auto":
        if detected and detected in LANGUAGES:
            return f"自动 · {LANGUAGES[detected]['short']}"
        return "自动"
    return LANGUAGES[code]["label"]


def build_prompt(text: str, pair: LangPair, template: str | None = None) -> str:
    """Build the request body sent to the provider.

    With no template a plain translation instruction is used. A custom
    template may reference ``{source}``, ``{target}`` (language prompt
    names) and ``{text}``; when ``{text}`` is absent the text is
    appended after a blank line.
    """
    src = LANGUAGES[pair.source]["prompt"]
    tgt = LANGUAGES[pair.target]["prompt"]
    if template:
        body = template.replace("{source}", src).replace("{target}", tgt)
        if "{text}" in template:
            return body.replace("{text}", text)
        return body + "\n\n" + text
    return (
        "You are a professional translator. Translate the following text "
        f"from {src} to {tgt}. Output ONLY the translation, nothing else — "
        "no explanations, no notes, no quotation marks.\n\n"
        + text
    )
