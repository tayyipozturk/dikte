"""Platform selection: macOS and Linux (GNOME/Ubuntu)."""

from __future__ import annotations

import sys

from .base import Platform


def get_platform() -> Platform:
    if sys.platform == "darwin":
        from .macos import MacPlatform

        return MacPlatform()
    if sys.platform.startswith("linux"):
        from .linux import LinuxPlatform

        return LinuxPlatform()
    raise RuntimeError(f"Dikte does not support {sys.platform} yet")
