"""Menu-bar item: renders the shared menu model as an NSMenu.

The menu is rebuilt every time it opens, so it never shows stale state.
Main thread only.
"""

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

from ...controller import Status
from ...ui import menu_model

log = logging.getLogger(__name__)


def _tint(name: str | None) -> NSColor | None:
    return {"red": NSColor.systemRedColor, "green": NSColor.systemGreenColor,
            "orange": NSColor.systemOrangeColor}.get(name, lambda: None)()


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
    def __init__(self, app, platform) -> None:  # noqa: ANN001 - AppCore, MacPlatform
        self._app = app
        self._platform = platform
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

    def refresh(self) -> None:
        self._render_icon()  # the menu itself is rebuilt when it opens

    def remove(self) -> None:
        NSStatusBar.systemStatusBar().removeStatusItem_(self._item)

    def perform(self, tag: int) -> None:
        action = self._actions.get(tag)
        if action is None:
            return
        try:
            action()
        except Exception:  # noqa: BLE001 - a menu action must never crash the app
            log.exception("Menu action failed")

    def _render_icon(self) -> None:
        symbol, tint, label = menu_model.PHASES.get(self._status.phase, menu_model.PHASES["idle"])
        button = self._item.button()
        button.setToolTip_(f"Dikte — {label}" + (f"\n{self._status.message}" if self._status.message else ""))
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

    def rebuild(self, menu: NSMenu) -> None:
        self._actions.clear()
        items = menu_model.build(
            self._app, self._status, self._history,
            hotkey_labels=self._platform.hotkey_labels,
            supports_hud=self._platform.supports_hud,
            paste_shortcuts=self._platform.paste_shortcut_choice,
        )
        self._render(menu, items)

    def _render(self, menu: NSMenu, items: list[menu_model.Item]) -> None:
        menu.removeAllItems()
        for entry in items:
            if entry.separator:
                menu.addItem_(NSMenuItem.separatorItem())
                continue
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                entry.title, "act:" if entry.action else None, ""
            )
            if entry.action is not None:
                tag = len(self._actions) + 1
                self._actions[tag] = entry.action
                item.setTag_(tag)
                item.setTarget_(self._target)
            item.setEnabled_(entry.enabled)
            if entry.checked is not None:
                item.setState_(NSControlStateValueOn if entry.checked else NSControlStateValueOff)
            if entry.submenu:
                submenu = NSMenu.alloc().initWithTitle_(entry.title)
                submenu.setAutoenablesItems_(False)
                self._render(submenu, list(entry.submenu))
                item.setSubmenu_(submenu)
            menu.addItem_(item)
