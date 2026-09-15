"""What kind of Linux desktop are we on, and what is allowed here."""

from __future__ import annotations

import grp
import os
import shutil
from dataclasses import dataclass


def session_type() -> str:
    """"wayland", "x11" or "unknown"."""
    value = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if value in ("wayland", "x11"):
        return value
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


def desktop() -> str:
    return os.environ.get("XDG_CURRENT_DESKTOP", "") or os.environ.get("DESKTOP_SESSION", "")


def in_input_group() -> bool:
    """Reading /dev/input (for a bare-key hotkey) needs the "input" group."""
    try:
        input_gid = grp.getgrnam("input").gr_gid
    except KeyError:
        return False
    return input_gid in os.getgroups()


def has_command(name: str) -> bool:
    return shutil.which(name) is not None


@dataclass(frozen=True)
class Session:
    type: str
    desktop: str
    input_group: bool
    has_xdotool: bool
    has_wl_copy: bool
    has_notify_send: bool

    @property
    def is_wayland(self) -> bool:
        return self.type == "wayland"

    @property
    def can_use_x11_tools(self) -> bool:
        return self.type == "x11" and self.has_xdotool


def detect() -> Session:
    return Session(
        type=session_type(),
        desktop=desktop(),
        input_group=in_input_group(),
        has_xdotool=has_command("xdotool"),
        has_wl_copy=has_command("wl-copy"),
        has_notify_send=has_command("notify-send"),
    )
