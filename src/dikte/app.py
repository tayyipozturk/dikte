"""Application wiring: menu bar, overlay, permissions, hotkey, audio, controller
and transcriber. DikteApp methods run on the main thread unless noted."""

from __future__ import annotations

import fcntl
import logging
import os
import signal
import subprocess
import sys
import threading
from logging.handlers import RotatingFileHandler

import AVFoundation
import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSColor,
    NSObject,
    NSTimer,
    NSWorkspace,
    NSWorkspaceDidWakeNotification,
)
from PyObjCTools import AppHelper, MachSignals

from . import login_item, models, paths, permissions
from .audio import AudioInput
from .config import Settings, load_settings, save_settings, updated
from .controller import Controller, Status
from .feedback import Sounds, notify
from .hotkey import KEY_LABELS, HotkeyListener
from .inserter import TextInserter
from .transcriber import JobResult, Transcriber
from .ui.hud import Hud
from .ui.menubar import MenuBar

log = logging.getLogger(__name__)

RESTART_EXIT_CODE = 75  # the launcher respawns the app on this code
TICK_S = 2.0
NO_PROMPTS = os.environ.get("DIKTE_NO_PROMPTS") == "1"  # smoke tests


class DikteApp:
    def __init__(self, settings: Settings, config_warnings: list[str]) -> None:
        self.settings = settings
        self.config_warnings = config_warnings
        self.paused = False
        self.banner = ""
        self._perms: permissions.Permissions | None = None
        self._secure_input = False
        self._devices: tuple[str, ...] = ()
        self._downloading = False

    # lifecycle -------------------------------------------------------------------

    def launch(self) -> None:
        self.sounds = Sounds()
        self.hud = Hud()
        self.menu = MenuBar(self)
        self.inserter = TextInserter()
        self.audio = AudioInput()
        self.transcriber = Transcriber(self.inserter, self._job_finished, self._engine_state)
        self.controller = Controller(self.settings, self.audio, self.transcriber, self.inserter.frontmost_pid, ui=self)
        self.audio.set_consumer(self.controller.push_frame)
        self.hotkey = HotkeyListener(self.settings.hotkey, self.controller.hotkey)
        self._discovery = AVFoundation.AVCaptureDeviceDiscoverySession.discoverySessionWithDeviceTypes_mediaType_position_(
            [AVFoundation.AVCaptureDeviceTypeMicrophone, AVFoundation.AVCaptureDeviceTypeExternal],
            AVFoundation.AVMediaTypeAudio, 0,
        )
        self._devices = self._device_names()
        self.controller.start()
        self.transcriber.start(self.settings)
        self._ask_permissions()
        if not self.hotkey.start():
            log.warning("Hotkey tap refused: Accessibility or Input Monitoring permission missing")
            if not NO_PROMPTS:
                permissions.request_input_monitoring()
        self.tick()
        if self.config_warnings:
            notify("Dikte", "Some settings were invalid and reset to defaults (see log).")
        log.info("Dikte started (mode=%s, hotkey=%s, engine=%s)", self.settings.mode, self.settings.hotkey,
                 self.settings.engine)

    def shutdown(self) -> None:
        log.info("Shutting down")
        for stop in (self.hotkey.stop, self.controller.stop, self.transcriber.stop, self.hud.hide):
            try:
                stop()
            except Exception:  # noqa: BLE001 - keep shutting down the rest
                log.exception("Shutdown step failed")

    def tick(self) -> None:
        self._perms = permissions.check()
        self._secure_input = permissions.secure_input_enabled()
        if self.hotkey.running:
            self.hotkey.ensure_enabled()
        elif self.hotkey.start():
            log.info("Hotkey active: %s", KEY_LABELS[self.settings.hotkey])
        names = self._device_names()
        if names != self._devices:
            log.info("Audio devices changed: %s", ", ".join(names) or "none")
            self._devices = names
            self.controller.command("devices_changed")

    def did_wake(self) -> None:
        # Event taps can silently die across sleep; recreate it and recheck devices.
        self.hotkey.stop()
        self.hotkey.start()
        self.controller.command("devices_changed")

    def _ask_permissions(self) -> None:
        if NO_PROMPTS:
            return
        perms = permissions.check()
        if perms.microphone == "undetermined":
            permissions.request_microphone()
        if not perms.accessibility:
            permissions.request_accessibility()

    # controller → UI (called from other threads) --------------------------------------

    def status_changed(self, status: Status) -> None:
        AppHelper.callAfter(self._show_status, status)

    def play(self, sound: str) -> None:
        if self.settings.sounds:
            AppHelper.callAfter(self.sounds.play, sound)

    def notify(self, message: str) -> None:
        notify("Dikte", message)

    def history_changed(self, items: tuple[str, ...]) -> None:
        AppHelper.callAfter(self.menu.set_history, items)

    def calibrated(self, threshold_db: float) -> None:
        AppHelper.callAfter(self.update_settings, auto_threshold=False, level_threshold_db=threshold_db)

    def _job_finished(self, result: JobResult) -> None:
        self.controller.job_finished(result)

    def _engine_state(self, ready: bool, message: str) -> None:
        self.controller.engine_state(ready, message)

    def _show_status(self, status: Status) -> None:
        self.menu.set_status(status)
        if not self.settings.show_hud:
            self.hud.hide()
        elif status.phase == "recording":
            if status.recording_s:
                seconds = int(status.recording_s)
                label = "Hands-free" if status.hands_free else "Recording"
                text = f"{label} {seconds // 60}:{seconds % 60:02d}"
            else:
                text = "Hearing you…"
            level = (status.level_db - (status.threshold_db - 15.0)) / 30.0
            self.hud.show(text, NSColor.systemRedColor(), min(1.0, max(0.0, level)))
        elif status.phase == "transcribing":
            self.hud.show("Transcribing…", NSColor.systemBlueColor(), None)
        else:
            self.hud.hide()

    # actions used by the menu ----------------------------------------------------------

    def warnings(self) -> list[tuple[str, object]]:
        items: list[tuple[str, object]] = []
        perms = self._perms
        if perms is not None:
            if perms.microphone == "undetermined":
                items.append(("Allow microphone access…", permissions.request_microphone))
            elif perms.microphone != "granted":
                items.append(("Allow microphone access…", lambda: permissions.open_settings("microphone")))
            if not perms.accessibility:
                items.append(("Allow Accessibility (needed to insert text)…", self._open_accessibility))
        if not self.hotkey.running:
            items.append(("Allow Input Monitoring (needed for the hotkey)…", self._open_input_monitoring))
        if self._secure_input:
            items.append(("Another app turned on Secure Keyboard Entry", None))
        if self.config_warnings:
            items.append(("Settings file had invalid values (see log)", self.open_settings_file))
        if not os.environ.get("DIKTE_LAUNCHER"):
            items.append(("Running outside Dikte.app: permissions belong to the terminal", None))
        return items

    def _open_accessibility(self) -> None:
        permissions.request_accessibility()
        permissions.open_settings("accessibility")

    def _open_input_monitoring(self) -> None:
        permissions.request_input_monitoring()
        permissions.open_settings("input_monitoring")

    def input_device_names(self) -> list[str]:
        return list(self._devices)

    def _device_names(self) -> tuple[str, ...]:
        with objc.autorelease_pool():
            return tuple(sorted(str(d.localizedName()) for d in self._discovery.devices()))

    def update_settings(self, **changes: object) -> None:
        try:
            new = updated(self.settings, **changes)
        except ValueError as exc:
            log.error("Rejected settings change %s: %s", changes, exc)
            return
        self._apply(new, persist=True)

    def _apply(self, new: Settings, persist: bool) -> None:
        old, self.settings = self.settings, new
        if persist:
            try:
                save_settings(new)
            except OSError as exc:
                notify("Dikte", f"Could not save settings: {exc}")
        if new.hotkey != old.hotkey:
            self.hotkey.set_hotkey(new.hotkey)
        if new.log_level != old.log_level:
            logging.getLogger().setLevel(new.log_level)
        if not new.show_hud:
            self.hud.hide()
        self.controller.command("settings", new)

    def reload_settings(self) -> None:
        new, self.config_warnings = load_settings()
        for warning in self.config_warnings:
            log.warning(warning)
        self._apply(new, persist=False)
        notify("Dikte", "Settings reloaded" + (" (with warnings, see log)" if self.config_warnings else ""))

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self.controller.command("pause" if self.paused else "resume")

    def cancel_recording(self) -> None:
        self.controller.command("cancel")

    def calibrate(self) -> None:
        self.controller.command("calibrate")

    def rescan_devices(self) -> None:
        self.controller.command("devices_changed")

    def insert_text(self, text: str) -> None:
        self.controller.command("insert_text", text)

    def copy_text(self, text: str) -> None:
        self.inserter.copy(text)

    def login_enabled(self) -> bool:
        return login_item.is_enabled()

    def toggle_login(self) -> None:
        try:
            login_item.disable() if login_item.is_enabled() else login_item.enable()
        except (OSError, RuntimeError) as exc:
            notify("Dikte", str(exc))

    def open_settings_file(self) -> None:
        path = paths.config_path()
        if not path.exists():
            save_settings(self.settings)
        if subprocess.run(["open", str(path)], check=False).returncode != 0:  # noqa: S603,S607
            subprocess.run(["open", "-t", str(path)], check=False)  # noqa: S603,S607

    def open_log(self) -> None:
        subprocess.run(["open", str(paths.log_dir() / "dikte.log")], check=False)  # noqa: S603,S607

    def restart(self) -> None:
        self.shutdown()
        if os.environ.get("DIKTE_LAUNCHER"):
            os._exit(RESTART_EXIT_CODE)
        os.execv(sys.executable, [sys.executable, "-m", "dikte"])  # noqa: S606

    def quit(self) -> None:
        NSApplication.sharedApplication().terminate_(None)

    def download_model(self, key: str) -> None:
        if self._downloading:
            return
        self._downloading = True
        info = models.CATALOG[key]
        last = [-1]

        def progress(done: int, total: int) -> None:
            percent = int(done * 100 / total) if total else 0
            if percent != last[0]:
                last[0] = percent
                AppHelper.callAfter(setattr, self, "banner", f"Downloading {key}: {percent}%")

        def work() -> None:
            try:
                models.download(info, progress)
                AppHelper.callAfter(self._download_done, key, "")
            except Exception as exc:  # noqa: BLE001 - report any failure to the user
                AppHelper.callAfter(self._download_done, key, str(exc))

        threading.Thread(target=work, name="dikte-download", daemon=True).start()

    def _download_done(self, key: str, error: str) -> None:
        self._downloading = False
        self.banner = ""
        if error:
            notify("Dikte", f"Download failed: {error}")
            return
        notify("Dikte", f"Model {key} is ready")
        self.update_settings(local_model=key)


class AppDelegate(NSObject):
    def initWithApp_(self, app):  # noqa: N802 - Objective-C selector
        self = objc.super(AppDelegate, self).init()
        if self is None:
            return None
        self._app = app
        return self

    def applicationDidFinishLaunching_(self, notification):  # noqa: N802
        self._safely(self._app.launch)
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(TICK_S, self, "tick:", None, True)
        center = NSWorkspace.sharedWorkspace().notificationCenter()
        center.addObserver_selector_name_object_(self, "didWake:", NSWorkspaceDidWakeNotification, None)

    def applicationWillTerminate_(self, notification):  # noqa: N802
        self._safely(self._app.shutdown)

    def tick_(self, timer):  # noqa: N802
        self._safely(self._app.tick)

    def didWake_(self, notification):  # noqa: N802
        self._safely(self._app.did_wake)

    @objc.python_method
    def _safely(self, fn) -> None:  # noqa: ANN001
        try:
            fn()
        except Exception:  # noqa: BLE001 - never let an exception reach AppKit
            log.exception("%s failed", getattr(fn, "__name__", fn))


def _setup_logging(level: str) -> None:
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(paths.log_dir() / "dikte.log", maxBytes=1_000_000, backupCount=3,
                                       encoding="utf-8")
    file_handler.setFormatter(formatter)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [file_handler, console]
    root.setLevel(level)


def _single_instance_lock() -> int | None:
    fd = os.open(paths.lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd  # held (and released by the OS) for the life of the process


def run() -> int:
    paths.ensure_dirs()
    settings, warnings = load_settings()
    _setup_logging(settings.log_level)
    lock = _single_instance_lock()
    if lock is None:
        log.info("Dikte is already running")
        return 0
    for warning in warnings:
        log.warning(warning)

    application = NSApplication.sharedApplication()
    application.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # menu bar only, no Dock icon
    dikte = DikteApp(settings, warnings)
    delegate = AppDelegate.alloc().initWithApp_(dikte)
    application.setDelegate_(delegate)
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        MachSignals.signal(signum, lambda _signum: application.terminate_(None))
    AppHelper.runEventLoop()
    return 0
