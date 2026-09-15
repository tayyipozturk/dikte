"""The shared menu structure and the control socket."""

import shutil
import tempfile
from pathlib import Path

import pytest

from dikte import ipc
from dikte.config import Settings, updated
from dikte.controller import Status
from dikte.ui import menu_model


class FakeApp:
    def __init__(self, settings=None):
        self.settings = settings or Settings()
        self.banner = ""
        self.paused = False
        self.calls: list[tuple] = []

    def warnings(self):
        return [("Allow microphone access…", lambda: self.calls.append(("fix",)))]

    def input_device_names(self):
        return ["USB Microphone", "Built-in"]

    def update_settings(self, **changes):
        self.calls.append(("update", changes))
        self.settings = updated(self.settings, **changes)

    def login_enabled(self):
        return False

    def __getattr__(self, name):  # every other menu action
        return lambda *args: self.calls.append((name, *args))


def titles(items):
    return [item.title for item in items]


def find(items, prefix):
    return next(item for item in items if item.title.startswith(prefix))


def test_menu_has_the_expected_sections():
    app = FakeApp()
    items = menu_model.build(app, Status(phase="idle"), (), hotkey_labels={"right_ctrl": "Right Ctrl"},
                             supports_hud=False)
    assert titles(items)[0] == "Dikte — Ready"
    assert any(t.startswith("⚠️") for t in titles(items))
    for section in ("Mode:", "Hotkey:", "Language:", "Microphone:", "Sensitivity", "Speech Engine:", "Output", "Recent"):
        find(items, section)
    assert "Quit Dikte" in titles(items)
    assert "Recording Overlay" not in titles(items)  # hidden where an overlay is impossible


def test_overlay_entry_only_with_hud_support():
    app = FakeApp()
    items = menu_model.build(app, Status(), (), hotkey_labels={}, supports_hud=True)
    assert "Recording Overlay" in titles(items)


def test_paste_shortcut_submenu_is_linux_only():
    app = FakeApp()
    without = find(menu_model.build(app, Status(), (), hotkey_labels={}, supports_hud=True), "Output")
    assert "Paste with" not in titles(without.submenu)
    with_choice = find(menu_model.build(app, Status(), (), hotkey_labels={}, supports_hud=False,
                                        paste_shortcuts=True), "Output")
    assert "Paste with" in titles(with_choice.submenu)


def test_menu_actions_change_settings():
    app = FakeApp()
    items = menu_model.build(app, Status(), (), hotkey_labels={"right_ctrl": "Right Ctrl"}, supports_hud=False)
    mode = find(items, "Mode:")
    voice = next(entry for entry in mode.submenu if entry.title.startswith("Voice"))
    voice.action()
    assert app.settings.mode == "voice"
    device = find(items, "Microphone:")
    next(entry for entry in device.submenu if entry.title == "USB Microphone").action()
    assert app.settings.input_device == "USB Microphone"


def test_recent_entries_reinsert_text():
    app = FakeApp()
    items = menu_model.build(app, Status(), ("Merhaba dünya",), hotkey_labels={}, supports_hud=False)
    recent = find(items, "Recent")
    next(entry for entry in recent.submenu if "Merhaba" in entry.title).action()
    assert ("insert_text", "Merhaba dünya") in app.calls


@pytest.fixture
def short_tmp():
    """Unix socket paths are limited to ~104 bytes, shorter than pytest's tmp_path."""
    directory = Path(tempfile.mkdtemp(dir="/tmp", prefix="dikte-test-"))  # noqa: S108
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def test_control_socket_round_trip(short_tmp):
    tmp_path = short_tmp
    received: list[str] = []

    def handler(command: str) -> str:
        received.append(command)
        return "ok" if command in ipc.COMMANDS else f"unknown command: {command}"

    path = tmp_path / "dikte.sock"
    server = ipc.ControlServer(path, handler)
    assert server.start()
    try:
        assert ipc.send(path, "toggle") == "ok"
        assert ipc.send(path, "ping") == "ok"  # answered by the server itself
        assert ipc.send(path, "nonsense").startswith("unknown")
        assert received == ["toggle", "nonsense"]
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        server.stop()
    assert not path.exists()
    assert ipc.send(path, "toggle") is None


def test_stale_socket_is_replaced(short_tmp):
    path = short_tmp / "dikte.sock"
    path.write_text("")  # left behind by a crash
    server = ipc.ControlServer(path, lambda _c: "ok")
    assert server.start()
    assert ipc.send(path, "toggle") == "ok"
    server.stop()
