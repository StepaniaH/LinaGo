"""Helpers for rendering several providers side by side."""

from __future__ import annotations

from linago.config import AppConfig, Provider


def resolve_providers(
    names: list[str],
    config: AppConfig,
    *,
    limit: int = 4,
) -> list[Provider]:
    """Providers named for comparison, deduplicated in declared order.

    Unknown names are skipped silently — a stale settings entry must
    never stop the popup from rendering. The result is capped because
    the popup has to fit on screen.
    """
    providers: list[Provider] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        try:
            providers.append(config.get(name))
        except KeyError:
            continue
        if len(providers) >= limit:
            break
    return providers


def pane_maxima(avail_h: int, count: int, *, min_h: int = 60) -> list[int]:
    """Split a vertical budget into per-pane height maxima.

    Panes share the budget evenly; small screens bottom out at the
    floor so every pane stays readable and scrolls internally.
    """
    if count <= 0:
        return []
    share = max(avail_h // count, min_h)
    return [share] * count
