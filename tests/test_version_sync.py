"""Guard against version and changelog drift."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_pyproject_version_matches_package():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    declared = data["project"]["version"]

    import linago

    assert declared == linago.__version__


def test_changelog_documents_current_version():
    import linago

    text = (ROOT / "CHANGELOG.md").read_text()
    current = f"## [{linago.__version__}]"
    assert current in text or "## [Unreleased]" in text, (
        f"CHANGELOG lacks a {current} heading"
    )
