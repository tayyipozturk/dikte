"""Menu-bar item. The menu is rebuilt every time it opens, from the current
settings and status, so it never shows stale state. Main thread only."""

from __future__ import annotations

import logging
from typing import Callable

import objc
from AppKit import (
    NSColor,
    NSControlStateValueOff,
    NSControlStateValueOn,
    NSImage,
    NSMenu,
    NSMenuItem,
    NSObject,
    NSStatusBar,
    NSVariableStatusItemLength,
)

from ..controller import Status
from ..hotkey import KEY_LABELS
from ..models import CATALOG, is_downloaded

log = logging.getLogger(__name__)

# phase: (SF Symbol, tint, label)
PHASES = {
    "starting": ("hourglass", None, "Loading…"),
    "idle": ("mic", None, "Ready"),
    "listening": ("waveform", "green", "Listening"),
    "recording": ("mic.fill", "red", "Recording"),
    "transcribing": ("ellipsis.circle", None, "Transcribing…"),
    "paused": ("mic.slash", None, "Paused"),
    "error": ("exclamationmark.triangle", "orange", "Needs attention"),
}
MODE_LABELS = {
    "hold": "Hold to talk (double-tap = hands-free)",
    "toggle": "Tap to start / stop (hold = push-to-talk)",
    "voice": "Voice activated (always listening)",
}
LANGUAGE_LABELS = {
    "tr": "Türkçe + English (recommended)",
    "auto": "Auto-detect (Turkish or English)",
    "en": "English only",
}
THRESHOLDS_DB = (-60, -55, -50, -45, -40, -35, -30)
SILENCE_STOPS_S = (1.5, 2.0, 3.0, 5.0, 0.0)


def _tint(name: str | None) -> NSColor | None:
    return {"red": NSColor.systemRedColor, "green": NSColor.systemGreenColor,
            "orange": NSColor.systemOrangeColor}.get(name, lambda: None)()


def _clip(text: str, limit: int = 70) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _hint(mode: str, hotkey: str) -> str:
    key = KEY_LABELS.get(hotkey, hotkey)
    return {
        "hold": f"Hold {key} to talk · double-tap for hands-free",
        "toggle": f"Tap {key} to start / stop · hold to talk",
        "voice": f"Just speak · tap {key} to pause listening",
    }.get(mode, "")


class _Target(NSObject):
    def initWithOwner_(self, owner):  # noqa: N802 - Objective-C selector
        self = objc.super(_Target, self).init()
        if self is None:
            return None
        self._owner = owner
        return self

    def act_(self, sender):  # noqa: N802
        self._owner.perform(int(sender.tag()))

    def menuNeedsUpdate_(self, menu):  # noqa: N802
        self._owner.rebuild(menu)


class MenuBar:
    def __init__(self, app) -> None:  # noqa: ANN001 - DikteApp (avoids an import cycle)
        self._app = app
        self._status = Status()
        self._history: tuple[str, ...] = ()
        self._actions: dict[int, Callable[[], None]] = {}
        self._icon_key: tuple | None = None
        self._target = _Target.alloc().initWithOwner_(self)
        self._menu = NSMenu.alloc().init()
        self._menu.setAutoenablesItems_(False)
        self._menu.setDelegate_(self._target)
        self._item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self._item.setMenu_(self._menu)
        self._render_icon()

    def set_status(self, status: Status) -> None:
        self._status = status
        self._render_icon()

    def set_history(self, items: tuple[str, ...]) -> None:
        self._history = items

    def perform(self, tag: int) -> None:
        action = self._actions.get(tag)
        if action is None:
            return
        try:
            action()
        except Exception:  # noqa: BLE001 - a menu action must never crash the app
            log.exception("Menu action failed")

    def _render_icon(self) -> None:
        symbol, tint, label = PHASES.get(self._status.phase, PHASES["idle"])
        button = self._item.button()
        tooltip = f"Dikte — {label}" + (f"\n{self._status.message}" if self._status.message else "")
        button.setToolTip_(tooltip)
        if (symbol, tint) == self._icon_key:
            return
        self._icon_key = (symbol, tint)
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, "Dikte")
        if image is not None:
            image.setTemplate_(True)
            button.setImage_(image)
        else:
            button.setTitle_("🎙")
        button.setContentTintColor_(_tint(tint))

    # building the menu ---------------------------------------------------------------

    def rebuild(self, menu: NSMenu) -> None:
        menu.removeAllItems()
        self._actions.clear()
        app, settings, status = self._app, self._app.settings, self._status
        self._add(menu, f"Dikte — {PHASES.get(status.phase, PHASES['idle'])[2]}")
        for line in (app.banner, status.message):
            if line:
                self._add(menu, "   " + _clip(line))
        self._add(menu, "   " + _hint(settings.mode, settings.hotkey))
        for text, action in app.warnings():
            self._add(menu, "⚠️  " + text, action)
        menu.addItem_(NSMenuItem.separatorItem())

        self._choices(menu, "Mode", MODE_LABELS, settings.mode, lambda v: app.update_settings(mode=v))
        self._choices(menu, "Hotkey", KEY_LABELS, settings.hotkey, lambda v: app.update_settings(hotkey=v))
        self._choices(menu, "Language", LANGUAGE_LABELS, settings.language, lambda v: app.update_settings(language=v))
        self._microphone_menu(menu)
        self._sensitivity_menu(menu)
        self._engine_menu(menu)
        self._output_menu(menu)
        self._recent_menu(menu)
        menu.addItem_(NSMenuItem.separatorItem())

        self._add(menu, "Resume Dikte" if app.paused else "Pause Dikte", app.toggle_pause)
        if status.phase == "recording":
            self._add(menu, "Cancel Recording", app.cancel_recording)
        menu.addItem_(NSMenuItem.separatorItem())
        self._add(menu, "Start at Login", app.toggle_login, checked=app.login_enabled())
        self._add(menu, "Sounds", lambda: app.update_settings(sounds=not settings.sounds), checked=settings.sounds)
        self._add(menu, "Recording Overlay", lambda: app.update_settings(show_hud=not settings.show_hud),
                  checked=settings.show_hud)
        self._add(menu, "Edit Settings File…", app.open_settings_file)
        self._add(menu, "Reload Settings", app.reload_settings)
        self._add(menu, "Open Log", app.open_log)
        self._add(menu, "Restart Dikte", app.restart)
        self._add(menu, "Quit Dikte", app.quit)

    def _microphone_menu(self, menu: NSMenu) -> None:
        settings, status = self._app.settings, self._status
        sub = self._submenu(menu, f"Microphone: {status.device or settings.input_device or 'system default'}")
        preferred = settings.input_device.casefold()
        names = self._app.input_device_names()
        self._add(sub, "System Default", lambda: self._app.update_settings(input_device=""), checked=not preferred)
        if preferred and not any(preferred in n.casefold() for n in names):
            self._add(sub, f"{settings.input_device} (not connected)", checked=True)
        for name in names:
            self._add(sub, name, lambda n=name: self._app.update_settings(input_device=n),
                      checked=bool(preferred) and preferred in name.casefold())
        sub.addItem_(NSMenuItem.separatorItem())
        self._add(sub, "Rescan Devices", self._app.rescan_devices)

    def _sensitivity_menu(self, menu: NSMenu) -> None:
        app, settings, status = self._app, self._app.settings, self._status
        sub = self._submenu(menu, "Sensitivity")
        if status.device:
            self._add(sub, f"Level {status.level_db:.0f} dB · threshold {status.threshold_db:.0f} dB · "
                           f"room {status.floor_db:.0f} dB")
        self._add(sub, "Automatic (follows room noise)",
                  lambda: app.update_settings(auto_threshold=not settings.auto_threshold),
                  checked=settings.auto_threshold)
        self._add(sub, "Calibrate Now (stay quiet 2 s)", app.calibrate)
        sub.addItem_(NSMenuItem.separatorItem())
        self._add(sub, "Minimum threshold" if settings.auto_threshold else "Threshold")
        for db in THRESHOLDS_DB:
            note = " (most sensitive)" if db == THRESHOLDS_DB[0] else " (least sensitive)" if db == THRESHOLDS_DB[-1] else ""
            self._add(sub, f"   {db} dB{note}", lambda d=db: app.update_settings(level_threshold_db=float(d)),
                      checked=round(settings.level_threshold_db) == db)
        sub.addItem_(NSMenuItem.separatorItem())
        self._add(sub, "Hands-free stops after silence of")
        for seconds in SILENCE_STOPS_S:
            label = f"   {seconds:g} s" if seconds else "   Never (tap to stop)"
            self._add(sub, label, lambda s=seconds: app.update_settings(silence_stop_s=s),
                      checked=settings.silence_stop_s == seconds)

    def _engine_menu(self, menu: NSMenu) -> None:
        app, settings = self._app, self._app.settings
        local = settings.engine == "local"
        sub = self._submenu(menu, f"Speech Engine: {'Local' if local else 'Cloud'}")
        self._add(sub, "Local: private and offline (whisper.cpp)", lambda: app.update_settings(engine="local"),
                  checked=local)
        self._add(sub, f"Cloud: {settings.cloud_model} (needs API key)", lambda: app.update_settings(engine="cloud"),
                  checked=not local)
        sub.addItem_(NSMenuItem.separatorItem())
        self._add(sub, "Local model")
        for key, info in CATALOG.items():
            ready = is_downloaded(info)
            title = f"   {key}: {info.note}" + ("" if ready else f" (download {info.size_mb} MB)")
            action = (lambda k=key: app.update_settings(local_model=k)) if ready else (lambda k=key: app.download_model(k))
            self._add(sub, title, action, checked=key == settings.local_model)
        sub.addItem_(NSMenuItem.separatorItem())
        self._add(sub, "Repair Skipped Speech (mixed languages)",
                  lambda: app.update_settings(repair_gaps=not settings.repair_gaps), checked=settings.repair_gaps)

    def _output_menu(self, menu: NSMenu) -> None:
        app, s = self._app, self._app.settings
        sub = self._submenu(menu, "Output")
        self._add(sub, "Paste (fast)", lambda: app.update_settings(insert_method="paste"),
                  checked=s.insert_method == "paste")
        self._add(sub, "Type Characters (no clipboard)", lambda: app.update_settings(insert_method="type"),
                  checked=s.insert_method == "type")
        sub.addItem_(NSMenuItem.separatorItem())
        for label, key in (
            ("Restore Clipboard After Paste", "restore_clipboard"),
            ("Add Space After Text", "trailing_space"),
            ("Press Enter After Inserting", "auto_enter"),
            ("Copy Instead If I Switched Apps", "paste_guard"),
            ("Esc Cancels Recording", "esc_cancels"),
            ("Keep Microphone Open (instant start)", "keep_mic_open"),
        ):
            value = getattr(s, key)
            self._add(sub, label, lambda k=key, v=value: app.update_settings(**{k: not v}), checked=value)

    def _recent_menu(self, menu: NSMenu) -> None:
        sub = self._submenu(menu, "Recent")
        if not self._history:
            self._add(sub, "Nothing yet")
            return
        self._add(sub, "Click to insert again")
        for text in self._history:
            self._add(sub, "   " + _clip(text, 60), lambda t=text: self._app.insert_text(t))
        sub.addItem_(NSMenuItem.separatorItem())
        self._add(sub, "Copy Last to Clipboard", lambda: self._app.copy_text(self._history[0]))

    # helpers -----------------------------------------------------------------------

    def _add(self, menu: NSMenu, title: str, action: Callable[[], None] | None = None,
             checked: bool | None = None) -> NSMenuItem:
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, "act:" if action else None, "")
        if action is not None:
            tag = len(self._actions) + 1
            self._actions[tag] = action
            item.setTag_(tag)
            item.setTarget_(self._target)
        item.setEnabled_(action is not None)
        if checked is not None:
            item.setState_(NSControlStateValueOn if checked else NSControlStateValueOff)
        menu.addItem_(item)
        return item

    def _submenu(self, menu: NSMenu, title: str) -> NSMenu:
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        sub = NSMenu.alloc().initWithTitle_(title)
        sub.setAutoenablesItems_(False)
        item.setSubmenu_(sub)
        menu.addItem_(item)
        return sub

    def _choices(self, menu: NSMenu, title: str, options: dict[str, str], current: str,
                 pick: Callable[[str], None]) -> None:
        sub = self._submenu(menu, f"{title}: {options.get(current, current).split(' (')[0]}")
        for value, label in options.items():
            self._add(sub, label, lambda v=value: pick(v), checked=value == current)
