"""macOS main loop: an accessory NSApplication (menu bar only, no Dock icon)."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Callable

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSObject,
    NSTimer,
    NSWorkspace,
    NSWorkspaceDidWakeNotification,
)
from PyObjCTools import AppHelper, MachSignals

log = logging.getLogger(__name__)

RESTART_EXIT_CODE = 75  # the launcher respawns the app on this code


class MacRuntime:
    def __init__(self, on_ready: Callable[[], None], on_quit: Callable[[], None]) -> None:
        self._on_ready = on_ready
        self._on_quit = on_quit
        self._wake: Callable[[], None] | None = None
        self._timers: list[NSTimer] = []
        self._delegate: _AppDelegate | None = None

    def run(self) -> int:
        application = NSApplication.sharedApplication()
        application.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        self._delegate = _AppDelegate.alloc().initWithRuntime_(self)
        application.setDelegate_(self._delegate)
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            MachSignals.signal(signum, lambda _signum: application.terminate_(None))
        AppHelper.runEventLoop()
        return 0

    def call_on_main(self, fn: Callable, *args: object) -> None:
        AppHelper.callAfter(fn, *args)

    def add_timer(self, seconds: float, fn: Callable[[], None]) -> None:
        def fire(_timer) -> None:  # noqa: ANN001 - NSTimer block
            try:
                fn()
            except Exception:  # noqa: BLE001 - never let it reach AppKit
                log.exception("Timer callback failed")

        self._timers.append(NSTimer.scheduledTimerWithTimeInterval_repeats_block_(seconds, True, fire))

    def set_wake_handler(self, fn: Callable[[], None]) -> None:
        self._wake = fn

    def quit(self) -> None:
        NSApplication.sharedApplication().terminate_(None)

    def restart(self) -> None:
        if os.environ.get("DIKTE_LAUNCHER"):
            os._exit(RESTART_EXIT_CODE)
        os.execv(sys.executable, [sys.executable, "-m", "dikte", *sys.argv[1:]])  # noqa: S606

    def open_path(self, path: Path) -> None:
        if subprocess.run(["open", str(path)], check=False).returncode != 0:  # noqa: S603,S607
            subprocess.run(["open", "-t", str(path)], check=False)  # noqa: S603,S607

    # called by the delegate ---------------------------------------------------------

    @objc.python_method
    def _ready(self) -> None:
        self._on_ready()

    @objc.python_method
    def _woke(self) -> None:
        if self._wake is not None:
            self._wake()


class _AppDelegate(NSObject):
    def initWithRuntime_(self, runtime):  # noqa: N802 - Objective-C selector
        self = objc.super(_AppDelegate, self).init()
        if self is None:
            return None
        self._runtime = runtime
        return self

    def applicationDidFinishLaunching_(self, notification):  # noqa: N802
        self._safely(self._runtime._ready)
        center = NSWorkspace.sharedWorkspace().notificationCenter()
        center.addObserver_selector_name_object_(self, "didWake:", NSWorkspaceDidWakeNotification, None)

    def applicationWillTerminate_(self, notification):  # noqa: N802
        self._safely(self._runtime._on_quit)

    def didWake_(self, notification):  # noqa: N802
        self._safely(self._runtime._woke)

    @objc.python_method
    def _safely(self, fn) -> None:  # noqa: ANN001
        try:
            fn()
        except Exception:  # noqa: BLE001 - never let an exception reach AppKit
            log.exception("%s failed", getattr(fn, "__name__", fn))
