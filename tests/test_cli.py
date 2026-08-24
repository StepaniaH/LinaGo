"""Tests for CLI argument handling and dependency policy."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

import linago.cli as cli

SETTINGS_TOML = """
[app]
provider = "prov_a"

[providers.prov_a]
type = "ollama"
label = "Local"
base_url = "http://127.0.0.1:11434"
model = "m1"

[providers.prov_b]
type = "openai"
label = "Cloud"
base_url = "https://b.test/v1"
model = "m2"
api_key_env = "PROV_B_KEY"
"""


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch) -> Path:
    """Isolated config dir so tests never read the developer's setup."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "settings.toml").write_text(SETTINGS_TOML)
    monkeypatch.setenv("LINAGO_CONFIG_DIR", str(cfg))
    return cfg


@pytest.fixture
def fake_ui(monkeypatch, config_dir: Path):
    """Stand-in for the GTK module; captures run_app() arguments."""
    captured: dict = {}

    def fake_run_app(source_text, **kwargs):
        captured["source_text"] = source_text
        captured.update(kwargs)
        return 0

    module = types.ModuleType("linago.ui")
    module.run_app = fake_run_app  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "linago.ui", module)
    return captured


class TestDependencyPolicy:
    def test_missing_capture_bins_exit(self, monkeypatch, capsys):
        monkeypatch.setattr(cli.shutil, "which", lambda name: None)
        with pytest.raises(SystemExit) as exc:
            cli.check_dependencies(need_capture=True, need_tesseract=False)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "slurp" in err and "grim" in err

    def test_vision_engine_does_not_need_tesseract(self, monkeypatch, capsys):
        which = {"slurp": "/usr/bin/slurp", "grim": "/usr/bin/grim"}
        monkeypatch.setattr(cli.shutil, "which", which.get)
        cli.check_dependencies(need_capture=True, need_tesseract=False)
        combined = capsys.readouterr()
        assert "tesseract" not in combined.out + combined.err

    def test_hyprctl_is_only_a_warning(self, monkeypatch, capsys):
        everything = {
            "slurp": "/usr/bin/slurp",
            "grim": "/usr/bin/grim",
            "tesseract": "/usr/bin/tesseract",
        }
        monkeypatch.setattr(cli.shutil, "which", lambda n: everything.get(n))
        cli.check_dependencies(need_capture=False, need_tesseract=False)
        assert "hyprctl" in capsys.readouterr().err


class TestParser:
    def test_defaults(self, config_dir):
        args = cli.build_parser(["prov_a", "prov_b"]).parse_args([])
        assert args.ocr is False
        assert args.translate is False
        assert args.from_lang == "auto"
        assert args.to_lang == "auto"
        assert args.provider is None

    def test_env_defaults(self, config_dir, monkeypatch):
        monkeypatch.setenv("TRANSLATE_FROM", "en")
        monkeypatch.setenv("TRANSLATE_TO", "zh")
        args = cli.build_parser(["prov_a"]).parse_args([])
        assert (args.from_lang, args.to_lang) == ("en", "zh")

    def test_provider_choices_enforced(self, config_dir):
        with pytest.raises(SystemExit):
            cli.build_parser(["prov_a"]).parse_args(["--provider", "nope"])


class TestMain:
    def test_text_mode_reaches_ui(self, fake_ui, config_dir):
        rc = cli.main(["--text", "hello world", "--translate"])
        assert rc == 0
        assert fake_ui["source_text"] == "hello world"
        assert fake_ui["translate"] is True
        assert fake_ui["pending_png"] is None
        assert fake_ui["provider_name"] == "prov_a"

    def test_ocr_mode_builds_runner(self, fake_ui, config_dir, tmp_path, monkeypatch):
        tools = {
            "slurp": "/usr/bin/slurp",
            "grim": "/usr/bin/grim",
            "hyprctl": "/usr/bin/hyprctl",
            "tesseract": "/usr/bin/tesseract",
        }
        monkeypatch.setattr(cli.shutil, "which", lambda n: tools.get(n))
        png = tmp_path / "shot.png"
        png.write_bytes(b"png")
        monkeypatch.setattr(cli.ocr_mod, "capture_region", lambda cache_dir: png)
        seen: dict = {}

        def fake_run_tesseract(path, langs="chi_sim+eng"):
            seen["path"] = path
            seen["langs"] = langs
            return "recognized text"

        monkeypatch.setattr(cli.ocr_mod, "run_tesseract", fake_run_tesseract)

        rc = cli.main(["--ocr"])
        assert rc == 0
        runner = fake_ui["ocr_runner"]
        assert callable(runner)
        assert runner() == "recognized text"
        assert seen["langs"] == "chi_sim+eng"

    def test_demo_mode_mentions_active_backend(self, fake_ui, config_dir):
        cli.main([])
        assert "Local · m1" in fake_ui["source_text"]
        assert fake_ui["translate"] is False
        assert fake_ui["provider_name"] == "prov_a"


class TestSelection:
    """Placeholder for the --selection mode landing next."""

    def test_selection_flag_not_yet_accepted(self, config_dir):
        with pytest.raises(SystemExit):
            cli.main(["--selection"])
