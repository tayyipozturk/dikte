"""The application, without any platform specifics.

Everything the desktop differs in (menu rendering, hotkeys, text insertion,
sounds, start at login, main loop) comes from a Platform object; see
dikte.platform. AppCore methods run on the UI thread unless noted.
"""

from __future__ import annotations

import fcntl
import logging
import os
import threading
from logging.handlers import RotatingFileHandler

from . import ipc, models, paths
from .audio import AudioInput
from .config import Settings, load_settings, save_settings, updated
from .controller import Controller, Status
from .platform import get_platform
from .platform.base import Platform, Runtime, Warning
from .transcriber import JobResult, Transcriber

log = logging.getLogger(__name__)

TICK_S = 2.0


class AppCore:
    def __init__(self, settings: Settings, config_warnings: list[str], platform: Platform) -> None:
        self.settings = settings
        self.config_warnings = config_warnings
        self.platform = platform
        self.runtime: Runtime | None = None  # set by run() before the main loop starts
        self.paused = False
        self.banner = ""
        self._downloading = False
        self._devices: tuple[str, ...] = ()

    # lifecycle ---------------------------------------------------------------------

    def launch(self) -> None:
        self.feedback = self.platform.feedback()
        self.hud = self.platform.hud()
        # Before the inserter: on Linux this registers the desktop entry, so the
        # first permission dialog can name the app.
        self.platform.request_permissions()
        self.inserter = self.platform.inserter()
        self.audio = AudioInput()
        self.transcriber = Transcriber(self.inserter, self._job_finished, self._engine_state)
        self.controller = Controller(self.settings, self.audio, self.transcriber, self.inserter.frontmost_pid, ui=self)
        self.audio.set_consumer(self.controller.push_frame)
        self.hotkey = self.platform.hotkey(self.settings.hotkey, self.controller.hotkey)
        self.control = ipc.ControlServer(paths.socket_path(), self._on_command)
        self._devices = tuple(self.platform.input_device_names())
        # Last: rendering the menu reads the hotkey and the other components above.
        self.tray = self.platform.tray(self)
        self.controller.start()
        self.transcriber.start(self.settings)
        self.control.start()
        self.runtime.add_timer(TICK_S, self.tick)
        set_wake_handler = getattr(self.runtime, "set_wake_handler", None)
        if set_wake_handler is not None:
            set_wake_handler(self.wake)
        if not self.hotkey.start():
            log.warning("Hotkey could not be registered (permission or desktop restriction)")
        self.tick()
        if self.config_warnings:
            self.notify("Some settings were invalid and reset to defaults (see log).")
        log.info("Dikte started on %s (mode=%s, hotkey=%s, engine=%s)", self.platform.name,
                 self.settings.mode, self.settings.hotkey, self.settings.engine)

    def shutdown(self) -> None:
        log.info("Shutting down")
        for stop in (self.hotkey.stop, self.control.stop, self.controller.stop, self.transcriber.stop, self.hud.hide):
            try:
                stop()
            except Exception:  # noqa: BLE001 - keep shutting down the rest
                log.exception("Shutdown step failed")

    def tick(self) -> None:
        if self.hotkey.running:
            self.hotkey.ensure_enabled()
        elif self.hotkey.start():
            log.info("Hotkey active: %s", self.platform.hotkey_labels.get(self.settings.hotkey, self.settings.hotkey))
        names = tuple(self.platform.input_device_names())
        if names != self._devices:
            log.info("Audio devices changed: %s", ", ".join(names) or "none")
            self._devices = names
            self.controller.command("devices_changed")
        self.tray.refresh()

    def wake(self) -> None:
        """After sleep: hotkey hooks can be dead and devices may have changed."""
        self.hotkey.stop()
        self.hotkey.start()
        self.controller.command("devices_changed")

    # controller → UI (called from other threads) ---------------------------------------

    def status_changed(self, status: Status) -> None:
        self.runtime.call_on_main(self._show_status, status)

    def play(self, sound: str) -> None:
        if self.settings.sounds:
            self.runtime.call_on_main(self.feedback.play, sound)

    def notify(self, message: str) -> None:
        self.feedback.notify("Dikte", message)

    def history_changed(self, items: tuple[str, ...]) -> None:
        self.runtime.call_on_main(self._set_history, items)

    def calibrated(self, threshold_db: float) -> None:
        self.runtime.call_on_main(
            self.update_settings, auto_threshold=False, level_threshold_db=threshold_db
        )

    def _job_finished(self, result: JobResult) -> None:
        self.controller.job_finished(result)

    def _engine_state(self, ready: bool, message: str) -> None:
        self.controller.engine_state(ready, message)

    def _set_history(self, items: tuple[str, ...]) -> None:
        self.tray.set_history(items)

    def _show_status(self, status: Status) -> None:
        self.tray.set_status(status)
        if not self.settings.show_hud or not self.platform.supports_hud:
            self.hud.hide()
        elif status.phase == "recording":
            if status.recording_s:
                seconds = int(status.recording_s)
                label = "Hands-free" if status.hands_free else "Recording"
                text = f"{label} {seconds // 60}:{seconds % 60:02d}"
            else:
                text = "Hearing you…"
            level = (status.level_db - (status.threshold_db - 15.0)) / 30.0
            self.hud.show(text, "recording", min(1.0, max(0.0, level)))
        elif status.phase == "transcribing":
            self.hud.show("Transcribing…", "transcribing", None)
        else:
            self.hud.hide()

    # control socket (`dikte toggle` from a desktop shortcut) ----------------------------

    def _on_command(self, command: str) -> str:
        if command in ("toggle", "cancel", "pause", "resume"):
            if command == "pause" and not self.paused or command == "resume" and self.paused:
                self.runtime.call_on_main(self.toggle_pause)
            else:
                self.controller.command(command)
            return "ok"
        return f"unknown command: {command}"

    # actions used by the menu ------------------------------------------------------------

    def warnings(self) -> list[Warning]:
        items = list(self.platform.warnings(self.hotkey.running))
        if self.config_warnings:
            items.append(("Settings file had invalid values (see log)", self.open_settings_file))
        return items

    def input_device_names(self) -> list[str]:
        return list(self._devices)

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
                self.notify(f"Could not save settings: {exc}")
        if new.hotkey != old.hotkey:
            self.hotkey.set_hotkey(new.hotkey)
        if new.log_level != old.log_level:
            logging.getLogger().setLevel(new.log_level)
        if not new.show_hud:
            self.hud.hide()
        self.controller.command("settings", new)
        self.tray.refresh()

    def reload_settings(self) -> None:
        new, self.config_warnings = load_settings()
        for warning in self.config_warnings:
            log.warning(warning)
        self._apply(new, persist=False)
        self.notify("Settings reloaded" + (" (with warnings, see log)" if self.config_warnings else ""))

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self.controller.command("pause" if self.paused else "resume")
        self.tray.refresh()

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
        return self.platform.login_item().is_enabled()

    def toggle_login(self) -> None:
        item = self.platform.login_item()
        try:
            item.disable() if item.is_enabled() else item.enable()
        except (OSError, RuntimeError) as exc:
            self.notify(str(exc))
        self.tray.refresh()

    def open_settings_file(self) -> None:
        path = paths.config_path()
        if not path.exists():
            save_settings(self.settings)
        self.runtime.open_path(path)

    def open_log(self) -> None:
        self.runtime.open_path(paths.log_dir() / "dikte.log")

    def restart(self) -> None:
        self.shutdown()
        self.runtime.restart()

    def quit(self) -> None:
        self.runtime.quit()

    def download_model(self, key: str) -> None:
        if self._downloading:
            return
        self._downloading = True
        info = models.CATALOG[key]
        last = [-1]
        runtime = self.runtime

        def progress(done: int, total: int) -> None:
            percent = int(done * 100 / total) if total else 0
            if percent != last[0]:
                last[0] = percent
                runtime.call_on_main(self._set_banner, f"Downloading {key}: {percent}%")

        def work() -> None:
            try:
                models.download(info, progress)
                runtime.call_on_main(self._download_done, key, "")
            except Exception as exc:  # noqa: BLE001 - report any failure to the user
                runtime.call_on_main(self._download_done, key, str(exc))

        threading.Thread(target=work, name="dikte-download", daemon=True).start()

    def _set_banner(self, text: str) -> None:
        self.banner = text
        self.tray.refresh()

    def _download_done(self, key: str, error: str) -> None:
        self._downloading = False
        self.banner = ""
        if error:
            self.notify(f"Download failed: {error}")
            return
        self.notify(f"Model {key} is ready")
        self.update_settings(local_model=key)


def setup_logging(level: str) -> None:
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(paths.log_dir() / "dikte.log", maxBytes=1_000_000, backupCount=3,
                                       encoding="utf-8")
    file_handler.setFormatter(formatter)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [file_handler, console]
    root.setLevel(level)


def single_instance_lock() -> int | None:
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
    setup_logging(settings.log_level)
    if single_instance_lock() is None:
        log.info("Dikte is already running")
        return 0
    for warning in warnings:
        log.warning(warning)
    platform = get_platform()
    app = AppCore(settings, warnings, platform)
    app.runtime = platform.runtime(on_ready=app.launch, on_quit=app.shutdown)
    return app.runtime.run()
