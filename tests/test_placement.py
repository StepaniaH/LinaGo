"""Tests for monitor geometry and popup placement math."""

from __future__ import annotations

import json

import linago.placement as placement
from linago.placement import (
    DEFAULT_MONITOR,
    Monitor,
    active_monitor,
    compute_placement,
    compute_section_caps,
    get_cursor_position,
    get_monitors,
)

FHD = Monitor(0, 0, 1920, 1080)


class TestComputePlacement:
    def test_center_grows_right_and_below(self):
        p = compute_placement(960, 540, FHD)
        assert p.horizontal == "right"
        assert p.vertical == "below"
        assert p.left_margin == 972
        assert p.top_margin == 552
        assert p.avail_h == 528

    def test_near_right_edge_flips_left(self):
        p = compute_placement(1900, 540, FHD)
        assert p.horizontal == "left"
        assert p.left_margin == 1408  # 1900 - 480 - 12

    def test_near_bottom_grows_above(self):
        p = compute_placement(960, 1060, FHD)
        assert p.vertical == "above"
        assert p.bottom_margin == 32  # 1080 - 1060 + 12
        assert p.avail_h == 1048

    def test_offset_monitor_converts_to_local_coords(self):
        right_screen = Monitor(x=1920, y=0, width=1920, height=1080)
        p = compute_placement(2000, 100, right_screen)
        assert p.left_margin == 92  # 2000 - 1920 + 12
        assert p.top_margin == 112

    def test_cursor_outside_monitor_is_clamped(self):
        right_screen = Monitor(x=1920, y=0, width=1920, height=1080)
        p = compute_placement(5000, 100, right_screen)
        assert p.horizontal == "left"  # clamped to right border
        assert p.left_margin == 1428  # 1920 - 480 - 12


class TestSectionCaps:
    def test_single_mode(self):
        src, trans = compute_section_caps(600, translate=False)
        assert trans == 0
        # chrome estimate is 146; budget 454 -> capped at SOURCE_MAX_H
        assert src == 280

    def test_translate_mode_splits_budget(self):
        src, trans = compute_section_caps(600, translate=True)
        # chrome estimate 238 -> budget 362 -> 40% / remainder
        assert src == 144
        assert trans == 218

    def test_measured_chrome_overrides_estimate(self):
        src, trans = compute_section_caps(420, translate=True, chrome_h=300)
        assert (src, trans) == (60, 80)  # minimum floors kick in

    def test_minimum_budget_floor(self):
        src, _trans = compute_section_caps(50, translate=False)
        assert src == 120


def _monitors_json(monitors: list[dict]) -> str:
    return json.dumps(monitors)


class TestHyprctlQueries:
    def test_get_monitors_parses_entries(self, monkeypatch):
        raw = _monitors_json(
            [
                {
                    "name": "DP-1",
                    "x": 0,
                    "y": 0,
                    "width": 3840,
                    "height": 2160,
                    "scale": 1.5,
                    "focused": False,
                },
                {
                    "name": "eDP-1",
                    "x": 2560,
                    "y": 0,
                    "width": 1920,
                    "height": 1080,
                    "scale": 1.0,
                    "focused": True,
                },
                {"broken": True},
            ]
        )
        monkeypatch.setattr(placement, "_run_hyprctl", lambda args: raw)
        monitors = get_monitors()
        assert len(monitors) == 2
        assert monitors[1].scale == 1.0
        assert monitors[1].focused is True

    def test_active_monitor_prefers_focused(self, monkeypatch):
        raw = _monitors_json(
            [
                {"name": "a", "x": 0, "y": 0, "width": 1, "height": 1},
                {
                    "name": "b",
                    "x": 1,
                    "y": 0,
                    "width": 2,
                    "height": 2,
                    "focused": True,
                },
            ]
        )
        monkeypatch.setattr(placement, "_run_hyprctl", lambda args: raw)
        assert active_monitor().name == "b"

    def test_fallback_when_hyprctl_missing(self, monkeypatch):
        monkeypatch.setattr(placement, "_run_hyprctl", lambda args: None)
        assert get_monitors() == []
        assert active_monitor() == DEFAULT_MONITOR

    def test_cursor_position_parses(self, monkeypatch):
        monkeypatch.setattr(placement, "_run_hyprctl", lambda args: "1234, 567\n")
        assert get_cursor_position() == (1234, 567)

    def test_cursor_position_none_on_failure(self, monkeypatch):
        monkeypatch.setattr(placement, "_run_hyprctl", lambda args: None)
        assert get_cursor_position() is None
