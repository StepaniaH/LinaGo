"""Comment-preserving reads and writes for the configuration files.

``tomllib`` (used by :mod:`linago.config`) stays the read path at
startup; this module exists for writers — the web console — which must
round-trip hand-written comments and formatting intact. Documents are
saved atomically (temp file + rename) and secrets files end up
readable by their owner only.
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile
from pathlib import Path

import tomlkit
from tomlkit.toml_document import TOMLDocument as Document

logger = logging.getLogger(__name__)


def load_document(path: Path) -> Document:
    """Parse *path* into an editable document; empty when missing."""
    if not path.exists():
        return tomlkit.document()
    with path.open("rb") as f:
        return tomlkit.load(f)


def save_document(path: Path, doc: Document) -> None:
    """Serialize *doc* over *path* atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = tomlkit.dumps(doc)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except OSError:
        _discard(tmp_name)
        raise


def set_secret(secrets_path: Path, name: str, key: str) -> None:
    """Upsert ``[keys].name = key`` in secrets.toml and restrict mode.

    The file is created when missing; after every write the mode is
    forced to owner-only so a fresh file never appears world-readable.
    """
    doc = load_document(secrets_path)
    if "keys" not in doc:
        doc["keys"] = tomlkit.table()
    doc["keys"][name] = key
    save_document(secrets_path, doc)
    if os.name == "posix":
        try:
            mode = stat.S_IMODE(secrets_path.stat().st_mode)
            if mode != 0o600:
                os.chmod(secrets_path, 0o600)
        except OSError:
            logger.warning("could not tighten %s permissions", secrets_path)


def remove_secret(secrets_path: Path, name: str) -> bool:
    """Drop ``[keys].name`` if present; True when something was removed."""
    doc = load_document(secrets_path)
    keys = doc.get("keys")
    if not isinstance(keys, dict) or name not in keys:
        return False
    del keys[name]
    save_document(secrets_path, doc)
    return True


def _discard(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
