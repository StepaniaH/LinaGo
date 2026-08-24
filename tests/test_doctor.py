"""Tests for the doctor self-check report."""

from __future__ import annotations

import subprocess
import types

from linago.doctor import has_fatal, run_checks

SETTINGS = """
[app]
provider = "prov_a"

[providers.prov_a]
type = "ollama"
base_url = "http://127.0.0.1:11434"
model = "m1"
"""


def _env(tmp_path, monkeypatch) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "settings.toml").write_text(SETTINGS)
    monkeypatch.setenv("LINAGO_CONFIG_DIR", str(cfg))


def _tools(**overrides):
    tools = {
        "slurp": "/usr/bin/slurp",
        "grim": "/usr/bin/grim",
        "tesseract": "/usr/bin/tesseract",
        "hyprctl": "/usr/bin/hyprctl",
    }
    tools.update(overrides)
    return lambda name: tools.get(name)


def test_report_shape_and_ok_state(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)

    def fake_runner(argv, **kw):
        return types.SimpleNamespace(stdout="chi_sim\neng\n", stderr="")

    checks = run_checks(
        probe=False,
        which=_tools(),
        runner=fake_runner,
        socket_path=str(tmp_path / "absent.sock"),
    )
    names = [c.name for c in checks]
    assert "config" in names and "version" in names and "daemon-socket" in names
    assert has_fatal(checks) is False


def test_missing_hard_binary_is_fatal(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    checks = run_checks(
        probe=False,
        which=lambda name: None,
        runner=subprocess.run,
        socket_path=str(tmp_path / "absent.sock"),
    )
    missing = [c for c in checks if c.name == "binary:slurp"]
    assert missing and missing[0].fatal


def test_tesseract_language_gap_is_listed(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    (tmp_path / "cfg" / "settings.toml").write_text(SETTINGS)

    def fake_runner(argv, **kw):
        return types.SimpleNamespace(stdout="eng\nosd\n", stderr="")

    checks = run_checks(
        probe=False,
        which=_tools(),
        runner=fake_runner,
        socket_path=str(tmp_path / "absent.sock"),
    )
    langs = next(c for c in checks if c.name == "tesseract-langs")
    assert langs.ok is False
    assert "chi_sim" in langs.detail
