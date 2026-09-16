"""Wayland text insertion through the desktop portals.

A Wayland desktop does not let an app type into other apps or put things on the
clipboard by itself. The supported route is a RemoteDesktop session with
clipboard access: the user approves "Allow remote interaction" once (the
approval is remembered through a restore token), after which Dikte can own the
clipboard and press Ctrl+V.

Everything here runs on the GLib main loop thread; callers from other threads
go through Runtime.call_on_main().
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import os
from pathlib import Path
from typing import Callable, Sequence

from gi.repository import Gio, GLib

from .keysyms import char_to_keysym

log = logging.getLogger(__name__)

BUS_NAME = "org.freedesktop.portal.Desktop"
OBJECT_PATH = "/org/freedesktop/portal/desktop"
REMOTE_DESKTOP = "org.freedesktop.portal.RemoteDesktop"
CLIPBOARD = "org.freedesktop.portal.Clipboard"
REQUEST = "org.freedesktop.portal.Request"

DEVICE_KEYBOARD = 1
PERSIST_UNTIL_REVOKED = 2
STATE_RELEASED, STATE_PRESSED = 0, 1
MIME_TYPES = ["text/plain;charset=utf-8", "text/plain", "UTF8_STRING"]
_KEY_GAP_MS = 15
_SELECTION_SETTLE_MS = 80
_RESPONSE_TIMEOUT_S = 90  # the user may take a while to answer the permission dialog


class PortalKeyboard:
    """RemoteDesktop + Clipboard session. Call start() on the main loop."""

    def __init__(self, token_path: Path) -> None:
        self._token_path = token_path
        self._counter = itertools.count(1)
        self._bus: Gio.DBusConnection | None = None
        self._sender = ""
        self._session = ""
        self._payload = b""
        self._transfer_subscription: int | None = None
        self.ready = False
        self.clipboard_enabled = False
        self.error = ""
        self._on_ready: Callable[[bool], None] | None = None

    # setup ---------------------------------------------------------------------

    def start(self, on_ready: Callable[[bool], None] | None = None) -> None:
        self._on_ready = on_ready
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as exc:
            self._fail(f"No session bus: {exc.message}")
            return
        self._sender = self._bus.get_unique_name()[1:].replace(".", "_")
        if self._transfer_subscription is None:  # subscribe once, even across retries
            self._transfer_subscription = self._bus.signal_subscribe(
                BUS_NAME, CLIPBOARD, "SelectionTransfer", OBJECT_PATH, None,
                Gio.DBusSignalFlags.NONE, self._on_selection_transfer,
            )
        options = {"session_handle_token": GLib.Variant("s", f"dikte{os.getpid()}")}
        self._request(REMOTE_DESKTOP, "CreateSession", "(a{sv})", (), options, self._created)

    def _created(self, response: int, results: dict) -> None:
        if response != 0:
            self._fail("The remote-interaction request was dismissed.")
            return
        self._session = results.get("session_handle", "")
        if not self._session.startswith("/"):
            self._fail("The portal did not return a session.")
            return
        options = {
            "types": GLib.Variant("u", DEVICE_KEYBOARD),
            "persist_mode": GLib.Variant("u", PERSIST_UNTIL_REVOKED),
        }
        token = self._load_token()
        if token:
            options["restore_token"] = GLib.Variant("s", token)
        self._request(REMOTE_DESKTOP, "SelectDevices", "(oa{sv})", (self._session,), options,
                      self._devices_selected)

    def _devices_selected(self, response: int, results: dict) -> None:
        if response != 0:
            self._fail("Keyboard access was not granted.")
            return
        self._call(CLIPBOARD, "RequestClipboard", GLib.Variant("(oa{sv})", (self._session, {})))
        self._request(REMOTE_DESKTOP, "Start", "(osa{sv})", (self._session, ""), {}, self._started)

    def _started(self, response: int, results: dict) -> None:
        if response != 0:
            self._fail("Remote interaction was denied. Dikte will copy text to the clipboard instead.")
            return
        self.clipboard_enabled = bool(results.get("clipboard_enabled", False))
        self._save_token(results.get("restore_token", ""))
        self.ready = True
        self.error = ""
        log.info("Portal keyboard ready (clipboard=%s)", self.clipboard_enabled)
        if self._on_ready:
            self._on_ready(True)

    def _fail(self, message: str) -> None:
        self.ready = False
        self.error = message
        log.warning("Portal keyboard unavailable: %s", message)
        if self._on_ready:
            self._on_ready(False)

    # actions -------------------------------------------------------------------

    def set_clipboard(self, text: str) -> bool:
        if not (self.ready and self.clipboard_enabled):
            return False
        self._payload = text.encode("utf-8")
        options = {"mime_types": GLib.Variant("as", MIME_TYPES)}
        return self._call(CLIPBOARD, "SetSelection", GLib.Variant("(oa{sv})", (self._session, options)))

    def press_combo(self, combo: Sequence[int], after: Callable[[], None] | None = None) -> None:
        """Press the keysyms in order, then release them in reverse."""
        steps = [(key, STATE_PRESSED) for key in combo] + [(key, STATE_RELEASED) for key in reversed(combo)]
        self._run_steps(steps, after)

    def type_text(self, text: str, after: Callable[[], None] | None = None) -> None:
        steps: list[tuple[int, int]] = []
        for char in text:
            keysym = char_to_keysym(char)
            steps += [(keysym, STATE_PRESSED), (keysym, STATE_RELEASED)]
        self._run_steps(steps, after)

    def _run_steps(self, steps: list[tuple[int, int]], after: Callable[[], None] | None) -> None:
        if not self.ready:
            return

        def step(index: int) -> bool:
            if index >= len(steps):
                if after:
                    after()
                return False
            keysym, state = steps[index]
            self._call(REMOTE_DESKTOP, "NotifyKeyboardKeysym",
                       GLib.Variant("(oa{sv}iu)", (self._session, {}, keysym, state)))
            GLib.timeout_add(_KEY_GAP_MS, step, index + 1)
            return False

        step(0)

    def paste_after_selection(self, combo: Sequence[int], after: Callable[[], None] | None = None) -> None:
        """Give the compositor a moment to notice the new clipboard owner."""

        def press() -> bool:
            self.press_combo(combo, after)
            return False

        GLib.timeout_add(_SELECTION_SETTLE_MS, press)

    # clipboard transfers --------------------------------------------------------

    def _on_selection_transfer(self, _connection, _sender, _path, _interface, _signal, params) -> None:  # noqa: ANN001
        session, mime_type, serial = params.unpack()
        if session != self._session:
            return
        try:
            result, fd_list = self._bus.call_with_unix_fd_list_sync(
                BUS_NAME, OBJECT_PATH, CLIPBOARD, "SelectionWrite",
                GLib.Variant("(ou)", (self._session, serial)), GLib.VariantType("(h)"),
                Gio.DBusCallFlags.NONE, 1000, None, None,
            )
            fd = fd_list.get(result.unpack()[0])
            with os.fdopen(fd, "wb") as stream:
                stream.write(self._payload)
            success = True
        except (GLib.Error, OSError) as exc:
            log.warning("Clipboard transfer failed (%s): %s", mime_type, exc)
            success = False
        self._call(CLIPBOARD, "SelectionWriteDone", GLib.Variant("(oub)", (self._session, serial, success)))

    # plumbing -------------------------------------------------------------------

    def _call(self, interface: str, method: str, params: GLib.Variant | None) -> bool:
        if self._bus is None:
            return False
        try:
            self._bus.call_sync(BUS_NAME, OBJECT_PATH, interface, method, params, None,
                                Gio.DBusCallFlags.NONE, 5000, None)
            return True
        except GLib.Error as exc:
            log.warning("%s.%s failed: %s", interface, method, exc.message)
            return False

    def _request(self, interface: str, method: str, signature: str, arguments: tuple, options: dict,
                 on_response: Callable[[int, dict], None]) -> None:
        """Portal methods answer through a Request object's Response signal."""
        assert self._bus is not None
        token = f"dikte{next(self._counter)}"
        options = dict(options, handle_token=GLib.Variant("s", token))
        request_path = f"{OBJECT_PATH}/request/{self._sender}/{token}"
        subscription: list[int] = []
        handled: list[bool] = [False]
        timeout_id: list[int] = []

        def handler(_connection, _sender, _path, _interface, _signal, signal_params) -> None:  # noqa: ANN001
            response, results = signal_params.unpack()
            if handled[0]:
                return
            handled[0] = True
            if timeout_id:
                GLib.source_remove(timeout_id[0])
            if subscription:
                self._bus.signal_unsubscribe(subscription[0])
            on_response(int(response), results)

        def on_timeout() -> bool:
            if handled[0]:
                return False
            handled[0] = True
            if subscription:
                self._bus.signal_unsubscribe(subscription[0])
            self._fail(f"{method} timed out")
            return False

        subscription.append(self._bus.signal_subscribe(BUS_NAME, REQUEST, "Response", request_path, None,
                                                       Gio.DBusSignalFlags.NONE, handler))
        full = GLib.Variant(signature, (*arguments, options))
        try:
            self._bus.call_sync(BUS_NAME, OBJECT_PATH, interface, method, full, None,
                                Gio.DBusCallFlags.NONE, 30000, None)
        except GLib.Error as exc:
            handled[0] = True  # no timeout afterwards: it would unsubscribe and fail twice
            self._bus.signal_unsubscribe(subscription[0])
            self._fail(f"{method} failed: {exc.message}")
            return

        timeout_id.append(GLib.timeout_add_seconds(_RESPONSE_TIMEOUT_S, on_timeout))

    def _load_token(self) -> str:
        try:
            return self._token_path.read_text().strip()
        except OSError:
            return ""

    def _save_token(self, token: str) -> None:
        if not token:
            return
        with contextlib.suppress(OSError):
            self._token_path.parent.mkdir(parents=True, exist_ok=True)
            self._token_path.write_text(token)
            self._token_path.chmod(0o600)
