"""Tray icon and menu on Linux (StatusNotifierItem via AppIndicator, GTK 3)."""

from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "3.0")
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
except ValueError:  # older systems
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3 as AppIndicator
from gi.repository import GLib, Gtk

from ...controller import Status
from ...ui import menu_model

log = logging.getLogger(__name__)

ICONS = {
    "starting": "content-loading-symbolic",
    "idle": "audio-input-microphone-symbolic",
    "listening": "microphone-sensitivity-high-symbolic",
    "recording": "media-record-symbolic",
    "transcribing": "content-loading-symbolic",
    "paused": "microphone-sensitivity-muted-symbolic",
    "error": "dialog-warning-symbolic",
}


def _menu_signature(items) -> tuple:  # noqa: ANN001 - list[menu_model.Item]
    """Compute a cheap signature of the rendered menu structure.

    Returns a recursive tuple of (title, checked, separator, sub-signature)
    for every item, allowing comparison to detect if the menu needs rebuilding.
    """
    sig = []
    for entry in items:
        if entry.separator:
            sig.append((None, None, True, ()))
        elif entry.checked is not None:
            sub_sig = _menu_signature(list(entry.submenu)) if entry.submenu else ()
            sig.append((entry.title, entry.checked, False, sub_sig))
        else:
            sub_sig = _menu_signature(list(entry.submenu)) if entry.submenu else ()
            sig.append((entry.title, None, False, sub_sig))
    return tuple(sig)


class LinuxTray:
    def __init__(self, app, platform) -> None:  # noqa: ANN001 - AppCore, LinuxPlatform
        self._app = app
        self._platform = platform
        self._status = Status()
        self._history: tuple[str, ...] = ()
        self._menu: Gtk.Menu | None = None
        self._menu_signature: tuple = ()
        self._indicator = AppIndicator.Indicator.new(
            "dikte", ICONS["idle"], AppIndicator.IndicatorCategory.APPLICATION_STATUS
        )
        self._indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self._indicator.set_title("Dikte")
        self.refresh()

    def set_status(self, status: Status) -> None:
        previous, self._status = self._status, status
        self._indicator.set_icon_full(ICONS.get(status.phase, ICONS["idle"]), f"Dikte — {status.phase}")
        label = ""
        if status.phase == "recording" and status.recording_s:
            seconds = int(status.recording_s)
            label = f"● {seconds // 60}:{seconds % 60:02d}"
        elif status.phase == "transcribing":
            label = "…"
        self._indicator.set_label(label, "● 0:00")
        if (status.phase, status.message) != (previous.phase, previous.message):
            self.refresh()  # the menu shows the phase and the last message

    def set_history(self, items: tuple[str, ...]) -> None:
        self._history = items
        self.refresh()

    def refresh(self) -> None:
        items = menu_model.build(
            self._app, self._status, self._history,
            hotkey_labels=self._platform.hotkey_labels,
            supports_hud=self._platform.supports_hud,
            paste_shortcuts=self._platform.paste_shortcut_choice,
        )
        sig = _menu_signature(items)
        if sig == self._menu_signature:
            return  # nothing visible changed, skip rebuild
        self._menu_signature = sig
        menu = self._render(items)
        menu.show_all()
        self._indicator.set_menu(menu)
        self._menu = menu  # keep a reference alive while it is shown

    def remove(self) -> None:
        self._indicator.set_status(AppIndicator.IndicatorStatus.PASSIVE)

    def _render(self, items) -> Gtk.Menu:  # noqa: ANN001 - list[menu_model.Item]
        menu = Gtk.Menu()
        for entry in items:
            if entry.separator:
                menu.append(Gtk.SeparatorMenuItem())
                continue
            if entry.checked is not None:
                item = Gtk.CheckMenuItem(label=entry.title)
                item.set_active(bool(entry.checked))  # before connecting: no spurious callback
            else:
                item = Gtk.MenuItem(label=entry.title)
            if entry.submenu:
                item.set_submenu(self._render(list(entry.submenu)))
            elif entry.action is not None:
                item.connect("activate", self._activated, entry.action)
            item.set_sensitive(entry.enabled)
            menu.append(item)
        return menu

    def _activated(self, _widget, action) -> None:  # noqa: ANN001
        def invoke_action() -> bool:
            try:
                action()
            except Exception:  # noqa: BLE001 - a menu action must never crash the app
                log.exception("Menu action failed")
            return False

        GLib.idle_add(invoke_action)
