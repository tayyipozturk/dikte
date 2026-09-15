"""Sounds and notifications on Linux (freedesktop sound theme + notify-send)."""

from __future__ import annotations

import logging
import subprocess

log = logging.getLogger(__name__)

# Sound ids from the freedesktop theme, which Ubuntu always ships.
SOUNDS = {
    "start": "message",
    "stop": "complete",
    "cancel": "dialog-warning",
    "error": "dialog-error",
    "listen": "device-added",
}


class LinuxFeedback:
    def __init__(self, player: str = "canberra-gtk-play") -> None:
        self._player = player

    def play(self, name: str) -> None:
        sound = SOUNDS.get(name)
        if sound is None:
            return
        self._spawn([self._player, "-i", sound, "-d", "dikte"])

    def notify(self, title: str, message: str) -> None:
        self._spawn(["notify-send", "-a", "Dikte", "-i", "audio-input-microphone",
                     title, message.replace("\x00", " ")[:300]])

    def _spawn(self, command: list[str]) -> None:
        try:
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603
        except OSError as exc:
            log.debug("%s unavailable: %s", command[0], exc)
