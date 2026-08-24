"""Tests for provider configuration and settings loading."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import linago.config as config
from linago.config import (
    load_actions,
    load_config,
    load_ocr_settings,
    load_settings,
    warn_secret_permissions,
)

SETTINGS_TOML = b"""
[app]
provider = "prov_b"

[providers.prov_a]
type = "ollama"
label = "Local"
base_url = "http://127.0.0.1:11434"
model = "m1"

[providers.prov_b]
type = "openai"
label = "Cloud"
base_url = "https://b.test/v1/"
model = "m2"
api_key_env = "PROV_B_KEY"

[providers.bad_type]
type = "mystery"
base_url = "https://x.test"
model = "m3"

[providers.no_model]
type = "openai"
base_url = "https://y.test/v1"

[ocr]
engine = "vision"
provider = "prov_a"

[actions]
explain = "Explain this {source} text:"
empty = "   "
"""


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "TRANSLATE_PROVIDER",
        "TRANSLATE_MODEL",
        "TRANSLATE_KEY_PROV_B",
        "PROV_B_KEY",
        "LINAGO_CONFIG_DIR",
    ):
        monkeypatch.delenv(var, raising=False)


def test_empty_settings_fall_back_to_ollama_default():
    cfg = load_config({})
    assert cfg.names() == ["ollama"]
    assert cfg.get().model == "qwen2.5:3b"


def test_providers_load_and_normalize():
    cfg = load_config({"providers": {}})
    assert cfg.active == "ollama"

    settings = _settings()
    cfg = load_config(settings)
    assert set(cfg.names()) == {"prov_a", "prov_b"}
    assert cfg.get("prov_b").base_url == "https://b.test/v1"  # rstrip("/")


def _settings() -> dict:
    return {
        "app": {"provider": "prov_b"},
        "providers": {
            "prov_a": {
                "type": "ollama",
                "label": "Local",
                "base_url": "http://127.0.0.1:11434",
                "model": "m1",
            },
            "prov_b": {
                "type": "openai",
                "label": "Cloud",
                "base_url": "https://b.test/v1/",
                "model": "m2",
                "api_key_env": "PROV_B_KEY",
            },
            "bad_type": {"type": "mystery", "base_url": "x", "model": "m"},
            "no_model": {"type": "openai", "base_url": "x"},
        },
        "ocr": {"engine": "vision", "provider": "prov_a"},
        "actions": {"explain": "Explain this {source} text:", "e": "  "},
    }


class TestKeyResolution:
    def test_env_var_named_by_api_key_env(self, monkeypatch):
        monkeypatch.setenv("PROV_B_KEY", "k-env")
        cfg = load_config(_settings())
        assert cfg.get("prov_b").api_key == "k-env"

    def test_secrets_win_over_env(self, monkeypatch):
        monkeypatch.setenv("PROV_B_KEY", "k-env")
        secrets = {"keys": {"prov_b": " k-secret "}}
        cfg = load_config(_settings(), secrets=secrets)
        assert cfg.get("prov_b").api_key == "k-secret"  # stripped

    def test_convention_variable_fallback(self, monkeypatch):
        monkeypatch.setenv("TRANSLATE_KEY_PROV_B", "k-conv")
        cfg = load_config(_settings())
        assert cfg.get("prov_b").api_key == "k-conv"

    def test_openai_without_key_is_not_ready(self):
        cfg = load_config(_settings())
        with pytest.raises(RuntimeError, match="API key"):
            cfg.get("prov_b").require_ready()


class TestOverrides:
    def test_unknown_active_falls_back_to_first(self):
        cfg = load_config({"app": {"provider": "ghost"}, **_settings()})
        assert cfg.active in cfg.providers

    def test_translated_model_env_overrides_active_only(self, monkeypatch):
        monkeypatch.setenv("TRANSLATE_MODEL", "m9")
        cfg = load_config(_settings())
        assert cfg.get("prov_b").model == "m9"
        assert cfg.get("prov_a").model == "m1"


def test_load_settings_reads_explicit_path(tmp_path: Path):
    path = tmp_path / "settings.toml"
    path.write_bytes(SETTINGS_TOML)
    settings = load_settings(path)
    assert settings["app"]["provider"] == "prov_b"
    assert load_ocr_settings(settings).engine == "vision"


def test_ocr_defaults():
    ocr = load_ocr_settings({})
    assert ocr.engine == "tesseract"
    assert ocr.tesseract_langs == "chi_sim+eng"
    assert ocr.provider is None


def test_ocr_invalid_engine_falls_back():
    ocr = load_ocr_settings({"ocr": {"engine": "psychic"}})
    assert ocr.engine == "tesseract"


def test_actions_drop_empty_templates():
    actions = load_actions(_settings())
    assert actions == {"explain": "Explain this {source} text:"}


class TestSecretPermissions:
    def test_warns_on_group_readable(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "config").mkdir()
        secret = tmp_path / "config" / "secrets.toml"
        secret.write_text("[keys]\n")
        os.chmod(secret, 0o644)
        monkeypatch.setattr(config, "find_config_dir", lambda: tmp_path / "config")
        warn_secret_permissions()
        assert "chmod 600" in capsys.readouterr().err

    def test_silent_when_restricted(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "config").mkdir()
        secret = tmp_path / "config" / "secrets.toml"
        secret.write_text("[keys]\n")
        os.chmod(secret, 0o600)
        monkeypatch.setattr(config, "find_config_dir", lambda: tmp_path / "config")
        warn_secret_permissions()
        assert capsys.readouterr().err == ""
