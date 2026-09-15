"""Linux (GNOME/Ubuntu) platform: tray menu, evdev hotkey, portal text insertion."""

from __future__ import annotations

import logging
from typing import Callable

from ... import paths
from ...audio import cached_input_device_names
from ..base import Action, NullHud, Warning
from . import session as session_module
from .feedback import LinuxFeedback
from .hotkey_evdev import KEY_LABELS, EvdevHotkey
from .inserter import LinuxInserter
from .login_item import LinuxLoginItem, install_desktop_entry

log = logging.getLogger(__name__)


class LinuxPlatform:
    name = "Linux"
    hotkey_labels = KEY_LABELS
    supports_hud = False  # Wayland does not allow an always-on-top overlay
    paste_shortcut_choice = True  # terminals need Ctrl+Shift+V

    def __init__(self) -> None:
        self.session = session_module.detect()
        self._runtime = None
        self._portal = None
        self._hotkey: EvdevHotkey | None = None
        self._feedback = LinuxFeedback()

    def runtime(self, on_ready: Action, on_quit: Action):  # noqa: ANN201
        from .runtime import LinuxRuntime

        self._runtime = LinuxRuntime(on_ready, on_quit)
        return self._runtime

    def tray(self, app: object):  # noqa: ANN201
        from .tray import LinuxTray

        return LinuxTray(app, self)

    def hud(self) -> NullHud:
        return NullHud()

    def hotkey(self, name: str, callback: Callable[[str, float], None]) -> EvdevHotkey:
        self._hotkey = EvdevHotkey(name, callback)
        return self._hotkey

    def inserter(self) -> LinuxInserter:
        if self.session.is_wayland:
            self._portal = self._start_portal()
        return LinuxInserter(self._runtime, self._portal, self.session, self._feedback.notify)

    def feedback(self) -> LinuxFeedback:
        return self._feedback

    def login_item(self) -> LinuxLoginItem:
        return LinuxLoginItem()

    def input_device_names(self) -> list[str]:
        return list(cached_input_device_names())

    def request_permissions(self) -> None:
        try:
            install_desktop_entry()  # identifies Dikte to the desktop and the portals
        except OSError as exc:
            log.warning("Could not write the desktop entry: %s", exc)

    def warnings(self, hotkey_running: bool) -> list[Warning]:
        items: list[Warning] = []
        if not hotkey_running:
            reason = (self._hotkey.reason if self._hotkey else "") or "The hotkey is not active."
            items.append((reason.splitlines()[0], self._show_hotkey_help))
        if self.session.is_wayland and self._portal is not None and not self._portal.ready:
            items.append(("Allow remote interaction so Dikte can paste…", self._retry_portal))
        if not self.session.is_wayland and not self.session.has_xdotool:
            items.append(("Install xdotool so Dikte can paste (sudo apt install xdotool)", None))
        return items

    def doctor(self) -> list[tuple[bool, str]]:
        session = self.session
        lines = [
            (session.type in ("wayland", "x11"), f"Session: {session.type} ({session.desktop or 'unknown desktop'})"),
            (session.input_group, "Keyboard access: member of the 'input' group "
                                  "(needed for a bare-key hotkey; sudo usermod -aG input $USER)"),
            (bool(session_module.has_command("canberra-gtk-play")), "Sound player (canberra-gtk-play)"),
            (session.has_notify_send, "Notifications (notify-send)"),
        ]
        if session.is_wayland:
            lines.append((session.has_wl_copy, "Clipboard tool (wl-copy) for the fallback path"))
        else:
            lines.append((session.has_xdotool, "xdotool (types and pastes on X11)"))
        lines.append((paths.desktop_entry_path().exists(), f"Desktop entry {paths.desktop_entry_path()}"))
        return lines

    # helpers ---------------------------------------------------------------------

    def _start_portal(self):  # noqa: ANN202
        from .portal import PortalKeyboard

        portal = PortalKeyboard(paths.data_dir() / "portal-restore-token")
        portal.start(self._portal_ready)
        return portal

    def _portal_ready(self, ok: bool) -> None:
        if not ok:
            self._feedback.notify("Dikte", "Without remote interaction Dikte can only copy text to the clipboard.")

    def _retry_portal(self) -> None:
        if self._portal is not None:
            self._portal.start(self._portal_ready)

    def _show_hotkey_help(self) -> None:
        reason = (self._hotkey.reason if self._hotkey else "") or "The hotkey is not active."
        self._feedback.notify("Dikte hotkey", reason)
