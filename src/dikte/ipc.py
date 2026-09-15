"""A tiny control socket so `dikte toggle` can reach the running app.

On Linux this lets a desktop keyboard shortcut start and stop a recording
without any special permissions.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import threading
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

COMMANDS = ("toggle", "cancel", "pause", "resume")
_TIMEOUT_S = 2.0
_MAX_COMMAND_BYTES = 128


class ControlServer:
    def __init__(self, path: Path, handler: Callable[[str], str]) -> None:
        self._path = path
        self._handler = handler
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> bool:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if self._path.exists():
                if send(self._path, "ping") is not None:
                    log.warning("Another Dikte already owns %s", self._path)
                    return False
                self._path.unlink()  # stale socket from a crashed run
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(self._path))
            os.chmod(self._path, 0o600)
            server.listen(4)
        except OSError as exc:
            log.warning("Control socket unavailable: %s", exc)
            return False
        self._socket = server
        self._running = True
        self._thread = threading.Thread(target=self._serve, name="dikte-ipc", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._running = False
        if self._socket is not None:
            with contextlib.suppress(OSError):
                self._socket.close()
        with contextlib.suppress(OSError):
            self._path.unlink()

    def _serve(self) -> None:
        assert self._socket is not None
        while self._running:
            try:
                connection, _ = self._socket.accept()
            except OSError:
                return  # closed by stop()
            with connection:
                try:
                    command = connection.recv(_MAX_COMMAND_BYTES).decode("utf-8", "replace").strip()
                    reply = "ok" if command == "ping" else self._handler(command)
                    connection.sendall(reply.encode("utf-8")[:_MAX_COMMAND_BYTES])
                except OSError:
                    continue
                except Exception:  # noqa: BLE001 - a bad command must not kill the server
                    log.exception("Control command failed")


def send(path: Path, command: str, timeout: float = _TIMEOUT_S) -> str | None:
    """Returns the reply, or None if no app is listening."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(path))
            client.sendall(command.encode("utf-8")[:_MAX_COMMAND_BYTES])
            return client.recv(_MAX_COMMAND_BYTES).decode("utf-8", "replace")
    except OSError:
        return None
