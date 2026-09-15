"""Linux platform logic. These modules are pure Python, so they run anywhere."""

import os
import struct

from dikte.platform.linux import keysyms, login_item, session
from dikte.platform.linux.hotkey_evdev import (
    _EVENT_FORMAT,
    KEYS,
    EvdevHotkey,
    keyboard_devices,
)

PROC_DEVICES = """I: Bus=0011 Vendor=0001 Product=0001 Version=ab41
N: Name="AT Translated Set 2 keyboard"
H: Handlers=sysrq kbd event3 leds
B: EV=120013

I: Bus=0003 Vendor=046d Product=c52b Version=0111
N: Name="Logitech Mouse"
H: Handlers=mouse0 event7
B: EV=17

I: Bus=0003 Vendor=05ac Product=0250 Version=0111
N: Name="USB Keyboard"
H: Handlers=kbd event11
B: EV=120013
"""


def test_keyboard_devices_finds_keyboards_only(tmp_path):
    source = tmp_path / "devices"
    source.write_text(PROC_DEVICES)
    assert keyboard_devices(source) == ["/dev/input/event3", "/dev/input/event11"]


def event(code: int, value: int) -> bytes:
    return struct.pack(_EVENT_FORMAT, 0, 0, 0x01, code, value)


def collect(events: bytes) -> list[str]:
    seen: list[str] = []
    hotkey = EvdevHotkey("right_ctrl", lambda kind, _t: seen.append(kind))
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, events)
        hotkey._files["fake"] = read_fd
        hotkey._read_device(read_fd)
    finally:
        os.close(write_fd)
        os.close(read_fd)
    return seen


def test_hotkey_reports_press_release_escape_and_other_keys():
    right_ctrl, other_key, escape = KEYS["right_ctrl"], 30, 1
    assert collect(event(right_ctrl, 1) + event(right_ctrl, 0)) == ["down", "up"]
    assert collect(event(right_ctrl, 1) + event(right_ctrl, 2) + event(right_ctrl, 0)) == ["down", "up"]  # no repeat
    assert collect(event(right_ctrl, 1) + event(other_key, 1)) == ["down", "other"]
    assert collect(event(other_key, 1)) == []  # typing is not reported while the hotkey is up
    assert collect(event(escape, 1)) == ["escape"]


def test_hotkey_ignores_keys_it_was_not_asked_about():
    seen: list[str] = []
    hotkey = EvdevHotkey("right_super", lambda kind, _t: seen.append(kind))
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, event(KEYS["right_ctrl"], 1))
        hotkey._files["fake"] = read_fd
        hotkey._read_device(read_fd)
    finally:
        os.close(write_fd)
        os.close(read_fd)
    assert seen == []


def test_session_detection(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    assert session.session_type() == "wayland"
    monkeypatch.setenv("XDG_SESSION_TYPE", "")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    assert session.session_type() == "x11"


def test_keysyms():
    assert keysyms.char_to_keysym("v") == 0x76
    assert keysyms.char_to_keysym("é") == 0xE9  # Latin-1 maps directly
    assert keysyms.char_to_keysym("ş") == 0x0100015F  # Unicode keysym range
    assert keysyms.char_to_keysym("\n") == keysyms.RETURN
    assert keysyms.PASTE_COMBOS["ctrl_shift_v"] == (keysyms.CONTROL_L, keysyms.SHIFT_L, keysyms.V)


def test_autostart_entry_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    item = login_item.LinuxLoginItem()
    assert not item.is_enabled()
    item.enable()
    text = (tmp_path / "config/autostart/dikte.desktop").read_text()
    assert "X-GNOME-Autostart-enabled=true" in text
    assert "-m dikte" in text
    assert item.is_enabled()
    item.disable()
    assert not item.is_enabled()
    entry = login_item.install_desktop_entry()
    assert entry.name == "local.dikte.Dikte.desktop" and entry.exists()
