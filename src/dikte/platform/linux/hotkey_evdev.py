"""Global hotkey by reading the keyboard devices directly (/dev/input/event*).

On a Wayland desktop this is the only way to get a real key-down/key-up for a
bare key such as Right Ctrl, which is what push-to-talk needs. It requires read
access to the input devices: on Ubuntu that means being in the "input" group.

Only three things are reported: the configured key going down or up, Esc, and
the fact that *some* other key was pressed (so a shortcut cancels a recording).
Which keys you type is never recorded, stored or logged.
"""

from __future__ import annotations

import contextlib
import glob
import logging
import os
import select
import struct
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# struct input_event { struct timeval time; __u16 type; __u16 code; __s32 value; }
_EVENT_FORMAT = "llHHi"
_EVENT_SIZE = struct.calcsize(_EVENT_FORMAT)
_EV_KEY = 0x01

# Linux key codes (include/uapi/linux/input-event-codes.h)
KEYS = {
    "right_ctrl": 97,
    "right_alt": 100,
    "right_shift": 54,
    "right_super": 126,
    "right_cmd": 126,  # so a config copied from macOS still works
    "right_option": 100,
    "fn": 464,
}
KEY_LABELS = {
    "right_ctrl": "Right Ctrl",
    "right_alt": "Right Alt (AltGr)",
    "right_shift": "Right Shift",
    "right_super": "Right Super",
}
_KEY_ESC = 1
_RESCAN_S = 5.0


def keyboard_devices(source: Path = Path("/proc/bus/input/devices")) -> list[str]:
    """Event devices that report keys, from /proc/bus/input/devices."""
    devices: list[str] = []
    try:
        blocks = source.read_text(errors="replace").split("\n\n")
    except OSError:
        return sorted(glob.glob("/dev/input/by-path/*-event-kbd"))
    for block in blocks:
        handlers = ""
        events = 0
        for line in block.splitlines():
            if line.startswith("H: Handlers="):
                handlers = line.partition("=")[2]
            elif line.startswith("B: EV="):
                with contextlib.suppress(ValueError):
                    events = int(line.partition("=")[2].strip(), 16)
        if "kbd" not in handlers or not events & (1 << _EV_KEY):
            continue
        for token in handlers.split():
            if token.startswith("event"):
                devices.append(f"/dev/input/{token}")
    return devices


class EvdevHotkey:
    def __init__(self, hotkey: str, callback: Callable[[str, float], None]) -> None:
        self._code = KEYS.get(hotkey, KEYS["right_ctrl"])
        self._callback = callback
        self._down = False
        self._files: dict[str, int] = {}
        self._wake_read = -1
        self._wake_write = -1
        self._thread: threading.Thread | None = None
        self._running = False
        self.reason = ""

    @property
    def running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def set_hotkey(self, hotkey: str) -> None:
        self._code = KEYS.get(hotkey, KEYS["right_ctrl"])
        if self._down:
            self._down = False
            self._emit("up")

    def start(self) -> bool:
        if self.running:
            return True
        devices = keyboard_devices()
        if not devices:
            self.reason = "No keyboard devices found under /dev/input."
            return False
        opened = self._open(devices)
        if not opened:
            self.reason = ("No permission to read the keyboard (/dev/input). Run:\n"
                           "    sudo usermod -aG input $USER\nthen log out and back in.")
            return False
        self._wake_read, self._wake_write = os.pipe()
        self._running = True
        self.reason = ""
        self._thread = threading.Thread(target=self._read_loop, name="dikte-hotkey", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._running = False
        if self._wake_write >= 0:
            with contextlib.suppress(OSError):
                os.write(self._wake_write, b"x")
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._close_all()
        for fd in (self._wake_read, self._wake_write):
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
        self._wake_read = self._wake_write = -1
        if self._down:
            self._down = False
            self._emit("up")

    def ensure_enabled(self) -> bool:
        return self.running

    # internals ----------------------------------------------------------------

    def _open(self, devices: list[str]) -> int:
        for path in devices:
            if path in self._files:
                continue
            try:
                self._files[path] = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except PermissionError:
                continue
            except OSError as exc:
                log.debug("Cannot open %s: %s", path, exc)
        return len(self._files)

    def _close_all(self) -> None:
        for fd in self._files.values():
            with contextlib.suppress(OSError):
                os.close(fd)
        self._files.clear()

    def _emit(self, kind: str) -> None:
        self._callback(kind, time.monotonic())

    def _read_loop(self) -> None:
        next_scan = time.monotonic() + _RESCAN_S
        while self._running:
            try:
                readable, _, _ = select.select([*self._files.values(), self._wake_read], [], [], 1.0)
            except (OSError, ValueError):
                break
            if self._wake_read in readable:
                break
            for fd in readable:
                self._read_device(fd)
            if time.monotonic() >= next_scan:  # keyboards plugged in later
                next_scan = time.monotonic() + _RESCAN_S
                self._open(keyboard_devices())

    def _read_device(self, fd: int) -> None:
        try:
            data = os.read(fd, _EVENT_SIZE * 64)
        except BlockingIOError:
            return
        except OSError:  # unplugged
            self._drop(fd)
            return
        for offset in range(0, len(data) - _EVENT_SIZE + 1, _EVENT_SIZE):
            _, _, kind, code, value = struct.unpack_from(_EVENT_FORMAT, data, offset)
            if kind != _EV_KEY or value == 2:  # ignore auto-repeat
                continue
            if code == self._code:
                down = value == 1
                if down != self._down:
                    self._down = down
                    self._emit("down" if down else "up")
            elif value == 1:
                if code == _KEY_ESC:
                    self._emit("escape")
                elif self._down:
                    self._emit("other")

    def _drop(self, fd: int) -> None:
        for path, open_fd in list(self._files.items()):
            if open_fd == fd:
                del self._files[path]
                with contextlib.suppress(OSError):
                    os.close(fd)
                return
