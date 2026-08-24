"""Cursor-anchored popup placement against monitor geometry.

Geometry comes from ``hyprctl``. All values are logical pixels:
``hyprctl monitors -j`` reports width/height with the monitor scale
already applied, and ``hyprctl cursorpos`` reports global logical
coordinates in the same space. Placement math therefore runs against
the active monitor's bounds with the cursor converted to monitor-local
coordinates, which keeps the popup correct on multi-monitor layouts.
Layer-shell margins are relative to the anchored output's edges, i.e.
monitor-local as well.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

SOURCE_MAX_H = 280
TRANSLATION_MAX_H = 500

# Rough chrome heights (px) used only before the popup can measure its
# real widget heights (see compute_section_caps).
HEADER_H = 56
FOOTER_H = 40
BODY_PAD_V = 28
SECTION_LABEL_H = 22
LANG_BAR_H = 46
SEPARATOR_H = 24

WIN_W = 480
GAP = 12


@dataclass(frozen=True)
class Monitor:
    x: int
    y: int
    width: int
    height: int
    scale: float = 1.0
    name: str = ""
    focused: bool = False


DEFAULT_MONITOR = Monitor(0, 0, 1920, 1080)


def _run_hyprctl(args: list[str]) -> str | None:
    try:
        return subprocess.check_output(["hyprctl", *args], text=True)
    except (OSError, subprocess.CalledProcessError):
        return None


def get_monitors() -> list[Monitor]:
    """Query hyprctl for the monitor list; empty on failure."""
    raw = _run_hyprctl(["monitors", "-j"])
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError:
        return []
    monitors: list[Monitor] = []
    for e in entries:
        try:
            monitors.append(
                Monitor(
                    x=int(e["x"]),
                    y=int(e["y"]),
                    width=int(e["width"]),
                    height=int(e["height"]),
                    scale=float(e.get("scale") or 1.0),
                    name=str(e.get("name", "")),
                    focused=bool(e.get("focused")),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return monitors


def active_monitor(monitors: list[Monitor] | None = None) -> Monitor:
    """The focused monitor, else the first known, else a sane default."""
    mons = get_monitors() if monitors is None else monitors
    for m in mons:
        if m.focused:
            return m
    if mons:
        return mons[0]
    return DEFAULT_MONITOR


def get_cursor_position() -> tuple[int, int] | None:
    """Global logical (x, y) of the pointer via hyprctl; None on failure."""
    out = _run_hyprctl(["cursorpos"])
    if not out:
        return None
    try:
        x_str, y_str = out.strip().split(",")
        return int(x_str.strip()), int(y_str.strip())
    except ValueError:
        return None


def default_chrome_h(translate: bool) -> int:
    """Estimated vertical chrome before real measurement is possible."""
    chrome = HEADER_H + FOOTER_H + BODY_PAD_V + SECTION_LABEL_H
    if translate:
        chrome += LANG_BAR_H + SEPARATOR_H + SECTION_LABEL_H
    return chrome


@dataclass(frozen=True)
class Placement:
    horizontal: str     # "right" | "left" — which side of the cursor we grow
    vertical: str       # "below" | "above" — which side of the cursor we grow
    left_margin: int    # monitor-local margins for layer-shell anchors
    top_margin: int     # meaningful when vertical == "below"
    bottom_margin: int  # meaningful when vertical == "above"
    avail_h: int        # usable vertical space in the chosen direction


def compute_placement(
    cursor_x: int,
    cursor_y: int,
    monitor: Monitor,
    win_w: int = WIN_W,
    gap: int = GAP,
) -> Placement:
    """Decide which corner of the cursor to grow the popup into.

    Rather than guessing a fixed popup height and flipping if it would
    overflow (fragile once content can grow after the fact — e.g. a long
    translation), anchor to whichever vertical direction has more room
    and let the caller clamp content height to what's actually
    available. The layer-shell anchor keeps that edge pinned as the
    popup grows/shrinks.
    """
    lx = min(max(cursor_x - monitor.x, 0), monitor.width)
    ly = min(max(cursor_y - monitor.y, 0), monitor.height)

    left = lx + gap
    horizontal = "right"
    if left + win_w > monitor.width:
        horizontal = "left"
        left = max(gap, lx - win_w - gap)

    space_below = max(monitor.height - ly - gap, 0)
    space_above = max(ly - gap, 0)
    if space_below >= space_above:
        vertical = "below"
        avail_h = space_below
    else:
        vertical = "above"
        avail_h = space_above
    avail_h = max(avail_h, 160)  # keep a usable minimum even near an edge

    return Placement(
        horizontal=horizontal,
        vertical=vertical,
        left_margin=left,
        top_margin=ly + gap,
        bottom_margin=max(gap, monitor.height - ly + gap),
        avail_h=avail_h,
    )


def compute_section_caps(
    avail_h: int,
    translate: bool,
    *,
    chrome_h: int | None = None,
) -> tuple[int, int]:
    """Split available vertical space between the source/translation
    scroll areas so the whole popup fits without overflowing the screen.

    ``chrome_h`` overrides the estimated chrome with measured widget
    heights when available. Returns (source_max_h, translation_max_h);
    translation_max_h is 0 when not in translate mode.
    """
    budget = max(avail_h - (chrome_h or default_chrome_h(translate)), 120)

    if not translate:
        return min(SOURCE_MAX_H, budget), 0

    source_cap = max(min(SOURCE_MAX_H, int(budget * 0.4)), 60)
    translation_cap = max(min(TRANSLATION_MAX_H, budget - source_cap), 80)
    return source_cap, translation_cap
