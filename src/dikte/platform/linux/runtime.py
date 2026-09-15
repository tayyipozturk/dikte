"""Linux main loop (GTK 3 / GLib)."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402 - must follow require_version

log = logging.getLogger(__name__)


class LinuxRuntime:
    def __init__(self, on_ready: Callable[[], None], on_quit: Callable[[], None]) -> None:
        self._on_ready = on_ready
        self._on_quit = on_quit
        self._quitting = False

    def run(self) -> int:
        GLib.idle_add(self._ready)
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, self._signalled)
        Gtk.main()
        return 0

    def call_on_main(self, fn: Callable, *args: object) -> None:
        def invoke() -> bool:
            try:
                fn(*args)
            except Exception:  # noqa: BLE001 - never kill the main loop
                log.exception("Main-thread callback failed")
            return False

        GLib.idle_add(invoke)

    def add_timer(self, seconds: float, fn: Callable[[], None]) -> None:
        def tick() -> bool:
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("Timer callback failed")
            return True

        GLib.timeout_add(int(seconds * 1000), tick)

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        try:
            self._on_quit()
        finally:
            Gtk.main_quit()

    def restart(self) -> None:
        os.execv(sys.executable, [sys.executable, "-m", "dikte"])  # noqa: S606

    def open_path(self, path: Path) -> None:
        try:
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603,S607
        except OSError as exc:
            log.warning("Could not open %s: %s", path, exc)

    def _ready(self) -> bool:
        try:
            self._on_ready()
        except Exception:  # noqa: BLE001
            log.exception("Start-up failed")
        return False

    def _signalled(self) -> bool:
        log.info("Received a termination signal")
        self.quit()
        return False
