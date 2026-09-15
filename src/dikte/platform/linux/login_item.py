"""Start at login on Linux, and the desktop entry that gives Dikte its identity."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from ... import paths

_ENTRY = """[Desktop Entry]
Type=Application
Name=Dikte
Comment=Speak and the text appears where your cursor is
Exec={command}
Icon=audio-input-microphone
Terminal=false
Categories=Utility;Accessibility;
StartupNotify=false
"""
_AUTOSTART_EXTRA = """X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=3
"""


def launch_command() -> str:
    return f"{sys.executable} -m dikte"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".dikte.", suffix=".desktop")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def install_desktop_entry() -> Path:
    """The desktop file is what identifies Dikte to the desktop and the portals."""
    path = paths.desktop_entry_path()
    _write(path, _ENTRY.format(command=launch_command()))
    return path


class LinuxLoginItem:
    def is_enabled(self) -> bool:
        return paths.autostart_path().exists()

    def enable(self) -> None:
        _write(paths.autostart_path(), _ENTRY.format(command=launch_command()) + _AUTOSTART_EXTRA)

    def disable(self) -> None:
        paths.autostart_path().unlink(missing_ok=True)
