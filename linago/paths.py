"""Runtime filesystem locations.

Transient state (screenshots, logs) lives under the XDG cache
directory; configuration is looked up in an explicit override dir, the
current checkout's ``config/``, then the user config dir.
"""

from __future__ import annotations

import os
from pathlib import Path


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "linago"


def config_search_dirs() -> list[Path]:
    """Config directories in lookup order."""
    dirs: list[Path] = []
    explicit = os.environ.get("LINAGO_CONFIG_DIR")
    if explicit:
        dirs.append(Path(explicit))
    dirs.append(Path.cwd() / "config")
    xdg = os.environ.get("XDG_CONFIG_HOME")
    user = Path(xdg) if xdg else Path.home() / ".config"
    dirs.append(user / "linago")
    return dirs


def find_config_dir() -> Path | None:
    """First directory on the search path that contains settings.toml."""
    for d in config_search_dirs():
        if (d / "settings.toml").exists():
            return d
    return None


def ensure_config_dir() -> Path:
    """An existing config directory, creating the user-level one if needed.

    Used by writers (theme regeneration, the web console) when no
    checkout-style ``config/`` directory exists.
    """
    existing = find_config_dir()
    if existing is not None:
        return existing
    xdg = os.environ.get("XDG_CONFIG_HOME")
    user = Path(xdg) if xdg else Path.home() / ".config"
    target = user / "linago"
    target.mkdir(parents=True, exist_ok=True)
    return target
