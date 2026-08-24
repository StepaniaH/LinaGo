"""Audio playback helpers for TTS output."""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

# Preferred players, first match wins. ffplay needs -nodisp -autoexit
# to behave like a plain audio player.
_PLAYERS = {
    "pw-play": [],
    "paplay": [],
    "aplay": [],
    "ffplay": ["-nodisp", "-autoexit"],
}


def pick_player(which: Callable[[str], str | None] = shutil.which) -> str | None:
    for name in _PLAYERS:
        if which(name):
            return name
    return None


def play_file(path: Path, player: str | None = None) -> bool:
    """Play an audio file detached; True when a player was found."""
    player = player or pick_player()
    if player is None:
        logger.warning("no audio player found (tried %s)", ", ".join(_PLAYERS))
        return False
    argv = [player, *_PLAYERS.get(player, []), str(path)]
    try:
        subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except OSError:
        logger.warning("failed to launch %s", player, exc_info=True)
        return False
