"""Global hotkey listener: a listen-only CGEventTap on its own run-loop thread.

Only the hotkey's up/down state, Esc, and the *fact* that some other key was
pressed are reported. Which key was typed is never recorded or logged.

A listen-only tap cannot delay or block typing, even if Python is busy.
Modifier (flagsChanged) events still arrive under Secure Keyboard Entry, so
modifier hotkeys keep working in password fields and secure terminals.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import Quartz

log = logging.getLogger(__name__)

# name: (virtual keycode, device-specific modifier flag bit)
KEYS = {
    "right_cmd": (54, 0x10),
    "right_option": (61, 0x40),
    "right_ctrl": (62, 0x2000),
    "right_shift": (60, 0x04),
    "fn": (63, 0x800000),
}
KEY_LABELS = {
    "right_cmd": "Right ⌘",
    "right_option": "Right ⌥",
    "right_ctrl": "Right ⌃",
    "right_shift": "Right ⇧",
    "fn": "fn / 🌐",
}
ESCAPE_KEYCODE = 53
SYNTHETIC_MARKER = 0x44494B54  # "DIKT", stamped on events Dikte posts itself

HotkeyCallback = Callable[[str, float], None]  # kind: down | up | other | escape


class HotkeyListener:
    def __init__(self, hotkey: str, callback: HotkeyCallback) -> None:
        self._keycode, self._mask = KEYS[hotkey]
        self._callback = callback
        self._down = False
        self._tap = None
        self._loop = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._ok = False

    @property
    def running(self) -> bool:
        return self._ok and self._thread is not None and self._thread.is_alive()

    def set_hotkey(self, hotkey: str) -> None:
        self._keycode, self._mask = KEYS[hotkey]
        if self._down:
            self._down = False
            self._emit("up")

    def start(self) -> bool:
        """Returns False if macOS refused the tap (permission missing)."""
        if self.running:
            return True
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="dikte-hotkey", daemon=True)
        self._thread.start()
        self._ready.wait(3.0)
        return self._ok

    def stop(self) -> None:
        loop, self._loop = self._loop, None
        if loop is not None:
            Quartz.CFRunLoopStop(loop)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._tap, self._ok = None, False
        if self._down:  # the release would be lost with the tap: never leave a recording stuck
            self._down = False
            self._emit("up")

    def ensure_enabled(self) -> bool:
        """macOS can silently disable taps (timeouts, sleep/wake). Re-enable,
        or stop if that fails (permission revoked) so start() can retry later."""
        tap = self._tap
        if tap is None or not self.running:
            return False
        if Quartz.CGEventTapIsEnabled(tap):
            return True
        Quartz.CGEventTapEnable(tap, True)
        if Quartz.CGEventTapIsEnabled(tap):
            log.warning("Hotkey tap was disabled by macOS; re-enabled it")
            return True
        log.warning("Hotkey tap cannot be enabled (permission revoked?)")
        self.stop()
        return False

    def _run(self) -> None:
        mask = Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged) | Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
        tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly, mask, self._handle, None,
        )
        if tap is not None:
            Quartz.CGEventTapEnable(tap, True)
            if not Quartz.CGEventTapIsEnabled(tap):
                # Without permission macOS hands out a tap that stays disabled.
                Quartz.CFMachPortInvalidate(tap)
                tap = None
        if tap is None:
            self._ok = False
            self._ready.set()
            return
        source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        loop = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(loop, source, Quartz.kCFRunLoopCommonModes)
        self._tap, self._loop, self._ok = tap, loop, True
        self._ready.set()
        Quartz.CFRunLoopRun()
        self._ok = False

    def _emit(self, kind: str) -> None:
        self._callback(kind, time.monotonic())

    def _handle(self, proxy, event_type, event, refcon):  # noqa: ANN001 - CoreGraphics callback
        try:
            if event_type in (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput):
                if self._tap is not None:
                    Quartz.CGEventTapEnable(self._tap, True)
                if self._down:  # we may have missed the release: never leave a recording stuck
                    self._down = False
                    self._emit("up")
                return event
            if Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUserData) == SYNTHETIC_MARKER:
                return event
            keycode = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
            if event_type == Quartz.kCGEventFlagsChanged:
                if keycode == self._keycode:
                    down = bool(Quartz.CGEventGetFlags(event) & self._mask)
                    if down != self._down:
                        self._down = down
                        self._emit("down" if down else "up")
                elif self._down:
                    self._emit("other")
            elif event_type == Quartz.kCGEventKeyDown:
                if keycode == ESCAPE_KEYCODE:
                    self._emit("escape")
                elif self._down:
                    self._emit("other")
        except Exception:  # noqa: BLE001 - never let an exception escape into CoreGraphics
            log.exception("Hotkey handler failed")
        return event
