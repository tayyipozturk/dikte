"""Putting the text where the cursor is, on Linux.

Wayland: the portal owns the clipboard and presses Ctrl+V (see portal.py).
X11: xdotool with xclip.
Neither: the text is copied and a notification asks you to paste it.

Terminals paste with Ctrl+Shift+V while everything else uses Ctrl+V, and a
Wayland app cannot see which window is focused, so the shortcut is a setting
(menu → Output → Paste with).
"""

from __future__ import annotations

import logging
import subprocess
import time

from ...config import Settings
from .keysyms import PASTE_COMBOS, PASTE_KEY_NAMES, RETURN
from .session import Session

log = logging.getLogger(__name__)

_XDOTOOL_DELAY_S = 0.05


class LinuxInserter:
    def __init__(self, runtime, portal, session: Session, notify) -> None:  # noqa: ANN001
        self._runtime = runtime
        self._portal = portal
        self._session = session
        self._notify = notify
        self._warned = False

    # capabilities ---------------------------------------------------------------

    def can_insert(self) -> bool:
        if self._session.can_use_x11_tools:
            return True
        return bool(self._portal is not None and self._portal.ready)

    def frontmost_pid(self) -> int | None:
        """Only possible on X11; Wayland keeps this private, so the paste guard is off."""
        if not self._session.can_use_x11_tools:
            return None
        try:
            output = subprocess.run(["xdotool", "getactivewindow", "getwindowpid"],  # noqa: S603,S607
                                    capture_output=True, text=True, timeout=2).stdout.strip()
            return int(output)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    # insertion ------------------------------------------------------------------

    def insert(self, text: str, settings: Settings, submit: bool) -> None:
        if text and settings.trailing_space and not submit and not text.endswith((" ", "\n")):
            text += " "
        if self._session.can_use_x11_tools:
            self._insert_x11(text, settings, submit)
        elif self._portal is not None and self._portal.ready:
            self._insert_portal(text, settings, submit)
        else:
            self._insert_fallback(text)

    def copy(self, text: str) -> None:
        if self._portal is not None and self._portal.ready and self._portal.clipboard_enabled:
            self._runtime.call_on_main(self._portal.set_clipboard, text)
            return
        self._copy_with_tool(text)

    # backends -------------------------------------------------------------------

    def _insert_portal(self, text: str, settings: Settings, submit: bool) -> None:
        portal, combo = self._portal, PASTE_COMBOS.get(settings.paste_shortcut, PASTE_COMBOS["ctrl_v"])

        def run() -> None:
            if text:
                if settings.insert_method == "type":
                    portal.type_text(text)
                elif portal.set_clipboard(text):
                    portal.paste_after_selection(combo)
                else:
                    portal.type_text(text)  # clipboard not granted
            if submit:
                portal.press_combo((RETURN,))

        self._runtime.call_on_main(run)

    def _insert_x11(self, text: str, settings: Settings, submit: bool) -> None:
        key = PASTE_KEY_NAMES.get(settings.paste_shortcut, "ctrl+v")
        if text and settings.insert_method == "type":
            self._xdotool(["type", "--clearmodifiers", "--delay", "4", "--", text])
        elif text:
            self._copy_with_tool(text)
            time.sleep(_XDOTOOL_DELAY_S)
            self._xdotool(["key", "--clearmodifiers", key])
        if submit:
            time.sleep(_XDOTOOL_DELAY_S)
            self._xdotool(["key", "--clearmodifiers", "Return"])

    def _insert_fallback(self, text: str) -> None:
        if not text:
            return
        self._copy_with_tool(text)
        reason = getattr(self._portal, "error", "") or "Dikte may not type into other windows here."
        if not self._warned:
            self._warned = True
            log.warning("Falling back to clipboard only: %s", reason)
        self._notify("Dikte", f"Text copied — press Ctrl+V to paste. ({reason})")

    def _copy_with_tool(self, text: str) -> None:
        command = (["wl-copy", "--type", "text/plain;charset=utf-8"] if self._session.has_wl_copy
                   else ["xclip", "-selection", "clipboard"])
        try:
            subprocess.run(command, input=text.encode("utf-8"), check=False, timeout=5)  # noqa: S603
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("Could not copy to the clipboard with %s: %s", command[0], exc)

    def _xdotool(self, arguments: list[str]) -> None:
        try:
            subprocess.run(["xdotool", *arguments], check=False, timeout=10)  # noqa: S603,S607
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("xdotool failed: %s", exc)
