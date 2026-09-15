"""Filesystem locations.

macOS uses ~/Library/…, Linux the XDG directories. Set DIKTE_HOME to put
config, models and logs in one folder instead (used by tests).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

APP_NAME = "Dikte"
APP_ID = "local.dikte.Dikte"  # macOS bundle id, Linux desktop-file / app id
BUNDLE_ID = APP_ID  # kept for the macOS code that speaks in bundle ids
IS_MACOS = sys.platform == "darwin"


def _home_override() -> Path | None:
    override = os.environ.get("DIKTE_HOME")
    return Path(override).expanduser() if override else None


def _xdg(variable: str, default: str) -> Path:
    value = os.environ.get(variable)
    return Path(value).expanduser() if value else Path.home() / default


def data_dir() -> Path:
    """Models and other large files."""
    override = _home_override()
    if override:
        return override
    if IS_MACOS:
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return _xdg("XDG_DATA_HOME", ".local/share") / "dikte"


def config_dir() -> Path:
    override = _home_override()
    if override:
        return override
    if IS_MACOS:
        return data_dir()
    return _xdg("XDG_CONFIG_HOME", ".config") / "dikte"


def app_support_dir() -> Path:  # legacy name used across the code base
    return data_dir()


def models_dir() -> Path:
    return data_dir() / "models"


def config_path() -> Path:
    return config_dir() / "config.json"


def lock_path() -> Path:
    return runtime_dir() / "dikte.lock"


def socket_path() -> Path:
    """Unix socket used by `dikte toggle` to reach the running app."""
    path = runtime_dir() / "dikte.sock"
    if len(str(path)) > 100:  # AF_UNIX paths are limited to ~104 bytes
        return Path(tempfile.gettempdir()) / f"dikte-{os.getuid()}.sock"
    return path


def runtime_dir() -> Path:
    override = _home_override()
    if override:
        return override
    if IS_MACOS:
        return data_dir()
    base = os.environ.get("XDG_RUNTIME_DIR")
    return Path(base) / "dikte" if base else Path(f"/tmp/dikte-{os.getuid()}")  # noqa: S108


def server_pid_path(instance: str = "app") -> Path:
    """One pid file per user of the server, so the CLI never touches the app's."""
    return runtime_dir() / f"whisper-server-{instance}.pid"


def launcher_conf_path() -> Path:
    return data_dir() / "launcher.conf"


def last_recording_path() -> Path:
    return data_dir() / "last-recording.wav"


def log_dir() -> Path:
    override = _home_override()
    if override:
        return override / "logs"
    if IS_MACOS:
        return Path.home() / "Library" / "Logs" / APP_NAME
    return _xdg("XDG_STATE_HOME", ".local/state") / "dikte" / "logs"


def launch_agent_path() -> Path:
    """macOS start-at-login entry."""
    return Path.home() / "Library" / "LaunchAgents" / f"{APP_ID}.plist"


def autostart_path() -> Path:
    """Linux start-at-login entry."""
    return _xdg("XDG_CONFIG_HOME", ".config") / "autostart" / "dikte.desktop"


def desktop_entry_path() -> Path:
    """Linux application entry (also gives the app its identity to the desktop)."""
    return _xdg("XDG_DATA_HOME", ".local/share") / "applications" / f"{APP_ID}.desktop"


def whisper_dir() -> Path:
    """Where the Linux installer unpacks the whisper.cpp binaries."""
    return data_dir() / "whisper"


def ensure_dirs() -> None:
    for directory in (data_dir(), config_dir(), models_dir(), log_dir(), runtime_dir()):
        directory.mkdir(parents=True, exist_ok=True)
