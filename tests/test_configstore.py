"""Tests for the comment-preserving configuration store."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import tomlkit

from linago.configstore import (
    load_document,
    remove_secret,
    save_document,
    set_secret,
)

COMMENTS_TOML = """\
# Active backend lives at the top so it is easy to find.
[app]
provider = "ollama"          # switched by the footer dropdown too

[providers.ollama]
type = "ollama"
label = "Ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen2.5:3b"
"""


class TestDocumentRoundTrip:
    def test_comments_and_order_survive_save(self, tmp_path: Path):
        target = tmp_path / "settings.toml"
        target.write_text(COMMENTS_TOML)

        doc = load_document(target)
        doc["app"]["provider"] = "deepseek"
        save_document(target, doc)

        text = target.read_text()
        assert "# Active backend lives at the top" in text
        assert 'provider = "deepseek"' in text
        assert text.index("# Active backend") < text.index("[providers.")
        # still valid TOML with untouched provider block
        reparsed = tomlkit.parse(text)
        assert reparsed["providers"]["ollama"]["model"] == "qwen2.5:3b"

    def test_missing_file_loads_empty(self, tmp_path: Path):
        doc = load_document(tmp_path / "nope.toml")
        assert len(doc) == 0
        doc["app"] = {"provider": "ollama"}
        save_document(tmp_path / "nope.toml", doc)
        assert (tmp_path / "nope.toml").exists()

    def test_save_is_atomic_no_temp_leftovers(self, tmp_path: Path):
        target = tmp_path / "settings.toml"
        doc = load_document(target)
        doc["app"] = {"provider": "x"}
        save_document(target, doc)
        leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".")]
        assert leftovers == []


class TestSecrets:
    def test_set_creates_file_owner_only(self, tmp_path: Path):
        path = tmp_path / "secrets.toml"
        set_secret(path, "openai", "k-test")
        doc = tomlkit.parse(path.read_text())
        assert doc["keys"]["openai"] == "k-test"
        if os.name == "posix":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_set_tightens_loose_mode(self, tmp_path: Path):
        path = tmp_path / "secrets.toml"
        path.write_text('[keys]\nopenai = "old"\n')
        os.chmod(path, 0o644)
        set_secret(path, "openai", "new")
        assert tomlkit.parse(path.read_text())["keys"]["openai"] == "new"
        if os.name == "posix":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_remove_missing_is_noop(self, tmp_path: Path):
        path = tmp_path / "secrets.toml"
        assert remove_secret(path, "ghost") is False

    def test_remove_existing_key(self, tmp_path: Path):
        path = tmp_path / "secrets.toml"
        set_secret(path, "openai", "k")
        assert remove_secret(path, "openai") is True
        assert "openai" not in tomlkit.parse(path.read_text())["keys"]
