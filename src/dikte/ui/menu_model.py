"""The menu as data, so macOS and Linux render the same structure."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..config import Settings
from ..controller import Status
from ..models import CATALOG, is_downloaded

PHASES = {
    "starting": ("hourglass", None, "Loading…"),
    "idle": ("mic", None, "Ready"),
    "listening": ("waveform", "green", "Listening"),
    "recording": ("mic.fill", "red", "Recording"),
    "transcribing": ("ellipsis.circle", None, "Transcribing…"),
    "paused": ("mic.slash", None, "Paused"),
    "error": ("exclamationmark.triangle", "orange", "Needs attention"),
}
MODE_LABELS = {
    "hold": "Hold to talk (double-tap = hands-free)",
    "toggle": "Tap to start / stop (hold = push-to-talk)",
    "voice": "Voice activated (always listening)",
}
LANGUAGE_LABELS = {
    "tr": "Türkçe + English (recommended)",
    "auto": "Auto-detect (Turkish or English)",
    "en": "English only",
}
PASTE_SHORTCUT_LABELS = {
    "ctrl_v": "Ctrl+V (editors, browsers)",
    "ctrl_shift_v": "Ctrl+Shift+V (terminals)",
    "shift_insert": "Shift+Insert (often works in both)",
}
THRESHOLDS_DB = (-60, -55, -50, -45, -40, -35, -30)
SILENCE_STOPS_S = (1.5, 2.0, 3.0, 5.0, 0.0)


@dataclass(frozen=True)
class Item:
    title: str = ""
    action: Callable[[], None] | None = None
    checked: bool | None = None
    submenu: tuple[Item, ...] = field(default_factory=tuple)
    separator: bool = False

    @property
    def enabled(self) -> bool:
        return self.action is not None or bool(self.submenu)


SEPARATOR = Item(separator=True)


def clip(text: str, limit: int = 70) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def hint(mode: str, hotkey_label: str) -> str:
    return {
        "hold": f"Hold {hotkey_label} to talk · double-tap for hands-free",
        "toggle": f"Tap {hotkey_label} to start / stop · hold to talk",
        "voice": f"Just speak · tap {hotkey_label} to pause listening",
    }.get(mode, "")


def _choices(title: str, options: dict[str, str], current: str,
             pick: Callable[[str], None]) -> Item:
    chosen = options.get(current, current).split(" (")[0]
    entries = tuple(
        Item(label, (lambda v=value: pick(v)), checked=value == current) for value, label in options.items()
    )
    return Item(f"{title}: {chosen}", submenu=entries)


def build(app, status: Status, history: tuple[str, ...], hotkey_labels: dict[str, str],
          supports_hud: bool, paste_shortcuts: bool = False) -> list[Item]:
    """app provides the actions (see AppCore); status/history come from the controller."""
    settings: Settings = app.settings
    items: list[Item] = [Item(f"Dikte — {PHASES.get(status.phase, PHASES['idle'])[2]}")]
    for line in (app.banner, status.message):
        if line:
            items.append(Item("   " + clip(line)))
    items.append(Item("   " + hint(settings.mode, hotkey_labels.get(settings.hotkey, settings.hotkey))))
    items += [Item("⚠️  " + text, action) for text, action in app.warnings()]
    items.append(SEPARATOR)

    items.append(_choices("Mode", MODE_LABELS, settings.mode, lambda v: app.update_settings(mode=v)))
    items.append(_choices("Hotkey", hotkey_labels, settings.hotkey, lambda v: app.update_settings(hotkey=v)))
    items.append(_choices("Language", LANGUAGE_LABELS, settings.language, lambda v: app.update_settings(language=v)))
    items.append(_microphone(app, settings, status))
    items.append(_sensitivity(app, settings, status))
    items.append(_engine(app, settings))
    items.append(_output(app, settings, paste_shortcuts))
    items.append(_recent(app, history))
    items.append(SEPARATOR)

    items.append(Item("Resume Dikte" if app.paused else "Pause Dikte", app.toggle_pause))
    if status.phase == "recording":
        items.append(Item("Cancel Recording", app.cancel_recording))
    items.append(SEPARATOR)
    items.append(Item("Start at Login", app.toggle_login, checked=app.login_enabled()))
    items.append(Item("Sounds", lambda: app.update_settings(sounds=not settings.sounds), checked=settings.sounds))
    if supports_hud:
        items.append(Item("Recording Overlay", lambda: app.update_settings(show_hud=not settings.show_hud),
                          checked=settings.show_hud))
    items += [
        Item("Edit Settings File…", app.open_settings_file),
        Item("Reload Settings", app.reload_settings),
        Item("Open Log", app.open_log),
        Item("Restart Dikte", app.restart),
        Item("Quit Dikte", app.quit),
    ]
    return items


def _microphone(app, settings: Settings, status: Status) -> Item:
    preferred = settings.input_device.casefold()
    names = app.input_device_names()
    entries = [Item("System Default", lambda: app.update_settings(input_device=""), checked=not preferred)]
    if preferred and not any(preferred in name.casefold() for name in names):
        entries.append(Item(f"{settings.input_device} (not connected)", checked=True))
    entries += [
        Item(name, (lambda n=name: app.update_settings(input_device=n)),
             checked=bool(preferred) and preferred in name.casefold())
        for name in names
    ]
    entries += [SEPARATOR, Item("Rescan Devices", app.rescan_devices)]
    current = status.device or settings.input_device or "system default"
    return Item(f"Microphone: {current}", submenu=tuple(entries))


def _sensitivity(app, settings: Settings, status: Status) -> Item:
    entries: list[Item] = []
    if status.device:
        entries.append(Item(f"Level {status.level_db:.0f} dB · threshold {status.threshold_db:.0f} dB · "
                            f"room {status.floor_db:.0f} dB"))
    entries.append(Item("Automatic (follows room noise)",
                        lambda: app.update_settings(auto_threshold=not settings.auto_threshold),
                        checked=settings.auto_threshold))
    entries.append(Item("Calibrate Now (stay quiet 2 s)", app.calibrate))
    entries.append(SEPARATOR)
    entries.append(Item("Minimum threshold" if settings.auto_threshold else "Threshold"))
    for db in THRESHOLDS_DB:
        note = " (most sensitive)" if db == THRESHOLDS_DB[0] else " (least sensitive)" if db == THRESHOLDS_DB[-1] else ""
        entries.append(Item(f"   {db} dB{note}", (lambda d=db: app.update_settings(level_threshold_db=float(d))),
                            checked=round(settings.level_threshold_db) == db))
    entries.append(SEPARATOR)
    entries.append(Item("Hands-free stops after silence of"))
    for seconds in SILENCE_STOPS_S:
        label = f"   {seconds:g} s" if seconds else "   Never (tap to stop)"
        entries.append(Item(label, (lambda s=seconds: app.update_settings(silence_stop_s=s)),
                            checked=settings.silence_stop_s == seconds))
    return Item("Sensitivity", submenu=tuple(entries))


def _engine(app, settings: Settings) -> Item:
    local = settings.engine == "local"
    entries = [
        Item("Local: private and offline (whisper.cpp)", lambda: app.update_settings(engine="local"), checked=local),
        Item(f"Cloud: {settings.cloud_model} (needs API key)", lambda: app.update_settings(engine="cloud"),
             checked=not local),
        SEPARATOR,
        Item("Local model"),
    ]
    for key, info in CATALOG.items():
        ready = is_downloaded(info)
        title = f"   {key}: {info.note}" + ("" if ready else f" (download {info.size_mb} MB)")
        action = (lambda k=key: app.update_settings(local_model=k)) if ready else (lambda k=key: app.download_model(k))
        entries.append(Item(title, action, checked=key == settings.local_model))
    entries += [
        SEPARATOR,
        Item("Repair Skipped Speech (mixed languages)",
             lambda: app.update_settings(repair_gaps=not settings.repair_gaps), checked=settings.repair_gaps),
    ]
    return Item(f"Speech Engine: {'Local' if local else 'Cloud'}", submenu=tuple(entries))


def _output(app, settings: Settings, paste_shortcuts: bool) -> Item:
    entries = [
        Item("Paste (fast)", lambda: app.update_settings(insert_method="paste"),
             checked=settings.insert_method == "paste"),
        Item("Type Characters (no clipboard)", lambda: app.update_settings(insert_method="type"),
             checked=settings.insert_method == "type"),
        SEPARATOR,
    ]
    if paste_shortcuts:
        entries.append(Item("Paste with"))
        entries += [
            Item(f"   {label}", (lambda v=value: app.update_settings(paste_shortcut=v)),
                 checked=settings.paste_shortcut == value)
            for value, label in PASTE_SHORTCUT_LABELS.items()
        ]
        entries.append(SEPARATOR)
    toggles = [
        ("Restore Clipboard After Paste", "restore_clipboard"),
        ("Add Space After Text", "trailing_space"),
        ("Press Enter After Inserting", "auto_enter"),
        ("Copy Instead If I Switched Apps", "paste_guard"),
        ("Esc Cancels Recording", "esc_cancels"),
        ("Keep Microphone Open (instant start)", "keep_mic_open"),
    ]
    entries += [
        Item(label, (lambda k=key, v=getattr(settings, key): app.update_settings(**{k: not v})),
             checked=getattr(settings, key))
        for label, key in toggles
    ]
    return Item("Output", submenu=tuple(entries))


def _recent(app, history: tuple[str, ...]) -> Item:
    if not history:
        return Item("Recent", submenu=(Item("Nothing yet"),))
    entries = [Item("Click to insert again")]
    entries += [Item("   " + clip(text, 60), (lambda t=text: app.insert_text(t))) for text in history]
    entries += [SEPARATOR, Item("Copy Last to Clipboard", lambda: app.copy_text(history[0]))]
    return Item("Recent", submenu=tuple(entries))
