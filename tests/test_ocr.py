"""Tests for tesseract OCR handling."""

from __future__ import annotations

import subprocess
import types
from pathlib import Path

import linago.ocr as ocr


def _make_png(tmp_path: Path) -> Path:
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG fake bytes")
    return png


def test_success_returns_normalized_text(monkeypatch, tmp_path):
    png = _make_png(tmp_path)
    monkeypatch.setattr(
        ocr.subprocess,
        "check_output",
        lambda *a, **kw: " hello\n\n\nworld \n",
    )
    assert ocr.run_tesseract(png) == "hello\nworld"
    assert not png.exists()


def test_failure_returns_none_and_removes_image(monkeypatch, tmp_path):
    png = _make_png(tmp_path)

    def boom(*a, **kw):
        raise subprocess.CalledProcessError(1, "tesseract")

    monkeypatch.setattr(ocr.subprocess, "check_output", boom)
    assert ocr.run_tesseract(png) is None
    assert not png.exists()


def test_empty_output_returns_empty_string(monkeypatch, tmp_path):
    """Empty result is distinct from failure: no crash, but nothing to do."""
    png = _make_png(tmp_path)
    monkeypatch.setattr(ocr.subprocess, "check_output", lambda *a, **kw: "  \n ")
    assert ocr.run_tesseract(png) == ""
    assert not png.exists()


def test_custom_language_pair_is_passed_through(monkeypatch, tmp_path):
    seen = {}

    def fake_check_output(argv, *a, **kw):
        seen["argv"] = argv
        return "text"

    monkeypatch.setattr(ocr.subprocess, "check_output", fake_check_output)
    ocr.run_tesseract(_make_png(tmp_path), langs="eng+chi_sim")
    assert "eng+chi_sim" in seen["argv"]


class TestForwardPolicy:
    def test_failure_is_never_translated(self):
        assert ocr.forward_to_translation(None) is False

    def test_empty_capture_is_never_translated(self):
        assert ocr.forward_to_translation("") is False
        assert ocr.forward_to_translation("   ") is False

    def test_recognized_text_is_forwarded(self):
        assert ocr.forward_to_translation("hello") is True


class TestPrimarySelection:
    def test_reads_stdout(self, monkeypatch):
        ns = types.SimpleNamespace(returncode=0, stdout=" picked \n")
        monkeypatch.setattr(ocr.subprocess, "run", lambda cmd, **kw: ns)
        assert ocr.read_primary_selection() == " picked \n"

    def test_empty_exit_code_means_none(self, monkeypatch):
        ns = types.SimpleNamespace(returncode=1, stdout="", stderr="empty")
        monkeypatch.setattr(ocr.subprocess, "run", lambda cmd, **kw: ns)
        assert ocr.read_primary_selection() is None

    def test_missing_tool_means_none(self, monkeypatch):
        def missing(cmd, **kw):
            raise FileNotFoundError("wl-paste")

        monkeypatch.setattr(ocr.subprocess, "run", missing)
        assert ocr.read_primary_selection() is None
