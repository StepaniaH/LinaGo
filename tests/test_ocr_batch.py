"""Tests for multi-region capture and batch OCR."""

from __future__ import annotations

import subprocess
from pathlib import Path

import linago.ocr as ocr
from linago.config import OcrSettings


def _fake_slurp(monkeypatch, coords_sequence):
    """Script slurp outputs; each entry is a region, None = cancel."""
    calls = {"n": 0}

    def fake_check_output(argv, *a, **kw):
        if argv and argv[0] == "slurp":
            idx = calls["n"]
            calls["n"] += 1
            if idx >= len(coords_sequence) or coords_sequence[idx] is None:
                raise subprocess.CalledProcessError(1, "slurp")
            return f"{coords_sequence[idx]} -1 10,10 100x50"
        raise AssertionError(f"unexpected command {argv}")

    monkeypatch.setattr(ocr.subprocess, "check_output", fake_check_output)
    return calls


def _fake_grim_ok(monkeypatch, tmp_path):
    def fake_run(argv, **kw):
        # grim writes the file at argv position after "-g", coords
        target = Path(argv[-1])
        target.write_bytes(b"png")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(ocr.subprocess, "run", fake_run)


class TestCaptureRegions:
    def test_collects_until_cancel(self, monkeypatch, tmp_path):
        _fake_grim_ok(monkeypatch, tmp_path)
        _fake_slurp(monkeypatch, ["r1", "r2", None, "never-reached"])
        paths = ocr.capture_regions(tmp_path / "cache")
        assert len(paths) == 2
        assert all(p.exists() for p in paths)

    def test_first_cancel_yields_empty(self, monkeypatch, tmp_path):
        _fake_slurp(monkeypatch, [None])
        paths = ocr.capture_regions(tmp_path / "cache")
        assert paths == []


class TestRunOcrBatch:
    def test_joins_non_empty_parts(self, monkeypatch, tmp_path):
        pngs = []
        for i in range(3):
            p = tmp_path / f"r{i}.png"
            p.write_bytes(b"png")
            pngs.append(p)
        results = {"r0": " first ", "r1": "", "r2": None}

        def fake_tess(argv, *a, **kw):
            key = Path(argv[1]).stem  # r0 / r1 / r2
            return results[key]

        monkeypatch.setattr(ocr.subprocess, "check_output", fake_tess)
        out = ocr.run_ocr_batch(
            pngs,
            engine="tesseract",
            ocr_cfg=OcrSettings(),
            get_provider=lambda name: None,
        )
        assert out == "first"
        assert not any(p.exists() for p in pngs)

    def test_all_empty_returns_empty_string(self, monkeypatch, tmp_path):
        png = tmp_path / "r.png"
        png.write_bytes(b"png")
        monkeypatch.setattr(ocr.subprocess, "check_output", lambda *a, **kw: "  \n")
        out = ocr.run_ocr_batch(
            [png],
            engine="tesseract",
            ocr_cfg=OcrSettings(),
            get_provider=lambda name: None,
        )
        assert out == ""

    def test_vision_engine_uses_provider(self, monkeypatch, tmp_path):
        png = tmp_path / "v.png"
        png.write_bytes(b"png")
        seen = {}

        class FakeProvider:
            pass

        def fake_vision(provider, path, **kw):
            seen["provider"] = provider
            seen["path"] = path
            return "vision text"

        monkeypatch.setattr(ocr, "vision_ocr", fake_vision)
        provider = FakeProvider()
        out = ocr.run_ocr_batch(
            [png],
            engine="vision",
            ocr_cfg=OcrSettings(),
            get_provider=lambda name: provider,
        )
        assert out == "vision text"
        assert seen["provider"] is provider
        assert not png.exists()  # cleaned up after its own pass
