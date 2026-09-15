"""Filesystem locations used by Dikte.

Everything lives under ~/Library/Application Support/Dikte (config, models) and
~/Library/Logs/Dikte (logs). Set DIKTE_HOME to relocate the support dir (tests).
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "Dikte"
BUNDLE_ID = "local.dikte.Dikte"


def app_support_dir() -> Path:
    override = os.environ.get("DIKTE_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / APP_NAME


def models_dir() -> Path:
    return app_support_dir() / "models"


def config_path() -> Path:
    return app_support_dir() / "config.json"


def lock_path() -> Path:
    return app_support_dir() / "dikte.lock"


def server_pid_path(instance: str = "app") -> Path:
    """One pid file per user of the server, so the CLI never touches the app's."""
    return app_support_dir() / f"whisper-server-{instance}.pid"


def launcher_conf_path() -> Path:
    return app_support_dir() / "launcher.conf"


def last_recording_path() -> Path:
    return app_support_dir() / "last-recording.wav"


def log_dir() -> Path:
    override = os.environ.get("DIKTE_HOME")
    if override:
        return Path(override).expanduser() / "logs"
    return Path.home() / "Library" / "Logs" / APP_NAME


def launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{BUNDLE_ID}.plist"


def ensure_dirs() -> None:
    for d in (app_support_dir(), models_dir(), log_dir()):
        d.mkdir(parents=True, exist_ok=True)
