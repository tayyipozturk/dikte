"""Audible feedback and system notifications."""

from __future__ import annotations

import logging
import subprocess

from AppKit import NSSound

log = logging.getLogger(__name__)

SOUNDS = {"start": "Tink", "stop": "Pop", "cancel": "Bottle", "error": "Basso", "listen": "Morse"}


class MacFeedback:
    """Sounds and notifications. Call play() from the main thread."""

    def __init__(self, volume: float = 0.35) -> None:
        self._volume = volume
        self._cache: dict[str, NSSound] = {}

    def notify(self, title: str, message: str) -> None:
        notify(title, message)

    def play(self, name: str) -> None:
        system_name = SOUNDS.get(name)
        if system_name is None:
            return
        sound = self._cache.get(system_name) or NSSound.soundNamed_(system_name)
        if sound is None:
            return
        self._cache[system_name] = sound
        sound.stop()
        sound.setVolume_(self._volume)
        sound.play()


def _applescript_string(text: str) -> str:
    text = "".join(ch if ch.isprintable() else " " for ch in text)[:300]  # no NUL/control chars
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(title: str, message: str) -> None:
    script = f"display notification {_applescript_string(message)} with title {_applescript_string(title)}"
    try:
        subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603,S607
    except (OSError, ValueError):
        log.warning("Could not show notification: %s", message)
