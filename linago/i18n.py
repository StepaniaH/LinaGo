"""UI translations.

Catalogs live under ``linago/locale/<lang>/LC_MESSAGES/linago.{po,mo}``.
Msgids are English; the zh catalog is committed alongside its compiled
.mo. Regenerate .mo after editing a .po with:

    msgfmt -o linago/locale/zh_CN/LC_MESSAGES/linago.mo \
        linago/locale/zh_CN/LC_MESSAGES/linago.po
"""

from __future__ import annotations

import gettext
import os
from pathlib import Path

_LOCALE_DIR = Path(__file__).resolve().parent / "locale"
_DOMAIN = "linago"

_current: gettext.NullTranslations = gettext.NullTranslations()


def _(msgid: str) -> str:
    """Translate msgid with the installed catalog (English fallback)."""
    return _current.gettext(msgid)


def install(lang: str | None = None) -> None:
    """Install the catalog for *lang*.

    Resolution: explicit argument, then ``LINAGO_LANG``, then the
    standard environment lookup (LANGUAGE/LC_ALL/LANG). Missing
    catalogs fall back to msgids (English).
    """
    global _current
    languages: list[str] | None
    if lang:
        languages = [lang]
    else:
        env_lang = os.environ.get("LINAGO_LANG")
        languages = [env_lang] if env_lang else None
    try:
        _current = gettext.translation(
            _DOMAIN,
            localedir=_LOCALE_DIR,
            languages=languages,
            fallback=True,
        )
    except OSError:
        _current = gettext.NullTranslations()
