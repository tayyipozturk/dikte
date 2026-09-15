"""macOS platform: menu-bar item, event-tap hotkey, clipboard paste, TCC permissions."""

from __future__ import annotations

import os
from typing import Callable

import AVFoundation
import objc

from ...paths import APP_ID  # noqa: F401 - kept so the bundle id lives in one place
from ..base import Action, Warning
from . import permissions
from .feedback import MacFeedback
from .hotkey import KEY_LABELS, HotkeyListener
from .hud import MacHud
from .inserter import TextInserter
from .login_item import MacLoginItem
from .menubar import MenuBar
from .runtime import MacRuntime

NO_PROMPTS = os.environ.get("DIKTE_NO_PROMPTS") == "1"  # smoke tests


class MacPlatform:
    name = "macOS"
    hotkey_labels = KEY_LABELS
    supports_hud = True
    paste_shortcut_choice = False  # ⌘V everywhere

    def __init__(self) -> None:
        self._discovery = AVFoundation.AVCaptureDeviceDiscoverySession.discoverySessionWithDeviceTypes_mediaType_position_(
            [AVFoundation.AVCaptureDeviceTypeMicrophone, AVFoundation.AVCaptureDeviceTypeExternal],
            AVFoundation.AVMediaTypeAudio, 0,
        )
        self._permissions: permissions.Permissions | None = None
        self._secure_input = False

    def runtime(self, on_ready: Action, on_quit: Action) -> MacRuntime:
        return MacRuntime(on_ready, on_quit)

    def tray(self, app: object) -> MenuBar:
        return MenuBar(app, self)

    def hud(self) -> MacHud:
        return MacHud()

    def hotkey(self, name: str, callback: Callable[[str, float], None]) -> HotkeyListener:
        return HotkeyListener(name, callback)

    def inserter(self) -> TextInserter:
        return TextInserter()

    def feedback(self) -> MacFeedback:
        return MacFeedback()

    def login_item(self) -> MacLoginItem:
        return MacLoginItem()

    def input_device_names(self) -> list[str]:
        with objc.autorelease_pool():
            return sorted(str(device.localizedName()) for device in self._discovery.devices())

    def request_permissions(self) -> None:
        if NO_PROMPTS:
            return
        current = permissions.check()
        if current.microphone == "undetermined":
            permissions.request_microphone()
        if not current.accessibility:
            permissions.request_accessibility()

    def warnings(self, hotkey_running: bool) -> list[Warning]:
        self._permissions = permissions.check()
        self._secure_input = permissions.secure_input_enabled()
        items: list[Warning] = []
        if self._permissions.microphone == "undetermined":
            items.append(("Allow microphone access…", permissions.request_microphone))
        elif self._permissions.microphone != "granted":
            items.append(("Allow microphone access…", lambda: permissions.open_settings("microphone")))
        if not self._permissions.accessibility:
            items.append(("Allow Accessibility (needed to insert text)…", _open_accessibility))
        if not hotkey_running:
            items.append(("Allow Input Monitoring (needed for the hotkey)…", _open_input_monitoring))
        if self._secure_input:
            items.append(("Another app turned on Secure Keyboard Entry", None))
        if not os.environ.get("DIKTE_LAUNCHER"):
            items.append(("Running outside Dikte.app: permissions belong to the terminal", None))
        return items

    def doctor(self) -> list[tuple[bool, str]]:
        current = permissions.check()
        return [
            (current.microphone == "granted", f"Microphone: {current.microphone}"),
            (current.accessibility, "Accessibility (insert text)"),
            (current.input_monitoring, "Input Monitoring (hotkey; may be covered by Accessibility)"),
            (not permissions.secure_input_enabled(), "Secure Keyboard Entry is off"),
        ]


def _open_accessibility() -> None:
    permissions.request_accessibility()
    permissions.open_settings("accessibility")


def _open_input_monitoring() -> None:
    permissions.request_input_monitoring()
    permissions.open_settings("input_monitoring")
