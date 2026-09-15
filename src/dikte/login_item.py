"""Start at login through a per-user LaunchAgent that runs Dikte.app's launcher."""

from __future__ import annotations

import os
import plistlib
import tempfile
from pathlib import Path

from . import paths

DEFAULT_APP = Path.home() / "Applications" / "Dikte.app"


def launcher_executable() -> Path | None:
    app = Path(os.environ["DIKTE_APP_PATH"]) if os.environ.get("DIKTE_APP_PATH") else DEFAULT_APP
    executable = app / "Contents" / "MacOS" / "Dikte"
    return executable if executable.exists() else None


def is_enabled() -> bool:
    return paths.launch_agent_path().exists()


def enable() -> None:
    executable = launcher_executable()
    if executable is None:
        raise RuntimeError("Dikte.app not found. Run scripts/install.sh first.")
    plist = {
        "Label": paths.BUNDLE_ID,
        "ProgramArguments": [str(executable)],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},  # restart after a crash, not after Quit
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": "Aqua",
    }
    target = paths.launch_agent_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".dikte.", suffix=".plist")
    try:
        with os.fdopen(fd, "wb") as handle:
            plistlib.dump(plist, handle)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def disable() -> None:
    # Takes effect at the next login; unloading now would kill the running app.
    paths.launch_agent_path().unlink(missing_ok=True)
