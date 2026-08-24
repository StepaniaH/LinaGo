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
action = "explain"

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

[actions]
explain = "Explain this {source} text:"
polish = "Polish the following text:\\n\\n{text}"
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


class TestActions:
    def test_default_action_from_settings(self, fake_ui, config_dir):
        cli.main(["--text", "hi", "--translate"])
        assert fake_ui["action_name"] == "explain"
        assert set(fake_ui["actions"]) == {"explain", "polish"}

    def test_explicit_action_wins(self, fake_ui, config_dir):
        cli.main(["--text", "hi", "--translate", "--action", "polish"])
        assert fake_ui["action_name"] == "polish"

    def test_unknown_action_is_rejected(self, fake_ui, config_dir, capsys):
        assert cli.main(["--text", "hi", "--action", "nope"]) == 2
        err = capsys.readouterr().err
        assert "nope" in err and "explain" in err
        assert fake_ui == {}


class TestSelection:
    def test_flag_accepted(self, config_dir):
        args = cli.build_parser(["prov_a"]).parse_args(["--selection"])
        assert args.selection is True

    def test_read_primary_selection_variants(self, monkeypatch):
        ok = types.SimpleNamespace(returncode=0, stdout=" picked \n", stderr="")
        empty = types.SimpleNamespace(returncode=1, stdout="", stderr="empty")

        monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kw: ok)
        assert cli.read_primary_selection() == " picked \n"

        monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kw: empty)
        assert cli.read_primary_selection() is None

        def missing(cmd, **kw):
            raise FileNotFoundError("wl-paste")

        monkeypatch.setattr(cli.subprocess, "run", missing)
        assert cli.read_primary_selection() is None

    def test_wl_paste_is_required(self, monkeypatch, capsys):
        monkeypatch.setattr(cli.shutil, "which", lambda name: None)
        with pytest.raises(SystemExit):
            cli.check_dependencies(need_selection=True)
        err = capsys.readouterr().err
        assert "wl-paste" in err

    def test_empty_selection_reports_and_skips_ui(
        self, fake_ui, config_dir, monkeypatch, capsys
    ):
        monkeypatch.setattr(cli.shutil, "which", lambda n: "/usr/bin/" + n)
        assert cli.main(["--selection"]) == 1
        assert fake_ui == {}
        assert "主选区" in capsys.readouterr().err

    def test_selection_reaches_ui_normalized(self, fake_ui, config_dir, monkeypatch):
        monkeypatch.setattr(cli.shutil, "which", lambda n: "/usr/bin/" + n)
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda cmd, **kw: types.SimpleNamespace(
                returncode=0, stdout="héllo\n\n\nworld\r\n", stderr=""
            ),
        )
        assert cli.main(["--selection", "--translate"]) == 0
        assert fake_ui["source_text"] == "héllo\nworld"
        assert fake_ui["translate"] is True


class TestOcrEngine:
    @staticmethod
    def _allow_bins(monkeypatch):
        tools = {
            "slurp": "/usr/bin/slurp",
            "grim": "/usr/bin/grim",
            "tesseract": "/usr/bin/tesseract",
            "hyprctl": "/usr/bin/hyprctl",
        }
        monkeypatch.setattr(cli.shutil, "which", lambda n: tools.get(n))

    def test_resolve_precedence(self):
        assert cli.resolve_ocr_engine(None, "tesseract") == "tesseract"
        assert cli.resolve_ocr_engine(None, "vision") == "vision"
        assert cli.resolve_ocr_engine("vision", "tesseract") == "vision"
        assert cli.resolve_ocr_engine("bogus", "vision") == "tesseract"

    def test_env_selects_engine(self, config_dir, monkeypatch):
        monkeypatch.setenv("TRANSLATE_OCR_ENGINE", "vision")
        args = cli.build_parser(["prov_a"]).parse_args([])
        assert args.ocr_engine == "vision"

    def test_vision_runner_skips_tesseract(
        self, fake_ui, config_dir, tmp_path, monkeypatch
    ):
        self._allow_bins(monkeypatch)
        png = tmp_path / "shot.png"
        png.write_bytes(b"png")
        monkeypatch.setattr(cli.ocr_mod, "capture_region", lambda cache: png)
        calls: dict = {"tess": 0, "vis": 0}

        def fake_tess(path, langs="chi_sim+eng"):
            calls["tess"] += 1
            return "T"

        def fake_vision(provider, path, **kw):
            calls["vis"] += 1
            assert provider.name == "prov_a"  # falls back to active
            return "V"

        monkeypatch.setattr(cli.ocr_mod, "run_tesseract", fake_tess)
        monkeypatch.setattr(cli, "vision_ocr", fake_vision)

        rc = cli.main(["--ocr", "--ocr-engine", "vision"])
        assert rc == 0
        assert fake_ui["ocr_runner"]() == "V"
        assert calls == {"tess": 0, "vis": 1}

    def test_tesseract_runner_default(self, fake_ui, config_dir, tmp_path, monkeypatch):
        self._allow_bins(monkeypatch)
        png = tmp_path / "shot.png"
        png.write_bytes(b"png")
        monkeypatch.setattr(cli.ocr_mod, "capture_region", lambda cache: png)
        monkeypatch.setattr(
            cli.ocr_mod, "run_tesseract", lambda path, langs="x": f"T:{langs}"
        )
        cli.main(["--ocr"])
        assert fake_ui["ocr_runner"]() == "T:chi_sim+eng"
