"""User settings: an immutable dataclass persisted as JSON.

Invalid or unknown values never crash the app. They fall back to the default
and are reported as warnings (shown in the menu and written to the log).
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Callable

from . import paths

MODES = ("hold", "toggle", "voice")
# The union of both platforms' keys; each platform offers its own subset in the menu.
HOTKEYS = ("right_cmd", "right_option", "right_ctrl", "right_shift", "fn", "right_alt", "right_super")
DEFAULT_HOTKEY = "right_cmd" if paths.IS_MACOS else "right_ctrl"
PASTE_SHORTCUTS = ("ctrl_v", "ctrl_shift_v", "shift_insert")
LANGUAGES = ("tr", "auto", "en")
ENGINES = ("local", "cloud")
INSERT_METHODS = ("paste", "type")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

# Whisper imitates the style and spelling of its prompt. A Turkish sentence that
# keeps English tech words in English strongly reduces "komponant"-style output.
DEFAULT_PROMPT_TR = (
    "Tamam, şimdi bu component'i refactor edelim. Let's also fix the bug in the "
    "API endpoint. Sonra testleri çalıştır, commit at ve pull request aç."
)
DEFAULT_PROMPT_EN = (
    "Okay, let's refactor this component, fix the bug in the API endpoint, "
    "run the tests and open a pull request."
)
DEFAULT_VOCABULARY = ("GitHub", "TypeScript", "JavaScript", "Python", "README", "JSON", "API")


@dataclass(frozen=True)
class Settings:
    # Trigger
    mode: str = "hold"  # hold (push-to-talk, double-tap = hands-free) | toggle | voice
    hotkey: str = DEFAULT_HOTKEY
    hold_threshold_ms: int = 200
    double_tap_ms: int = 350
    esc_cancels: bool = True  # Esc discards a recording in progress
    # Microphone
    input_device: str = ""  # part of the device name, e.g. "USB"; "" = system default
    mic_linger_s: float = 3.0  # keep the mic open briefly after a recording
    keep_mic_open: bool = False  # zero start-up delay, but the orange mic dot stays on
    # Sound level / voice detection
    auto_threshold: bool = True  # follow room noise: threshold = noise floor + margin
    noise_margin_db: float = 10.0
    level_threshold_db: float = -45.0  # manual threshold, and the floor for auto
    silence_stop_s: float = 3.0  # hands-free: stop after this much silence (0 = never)
    voice_silence_stop_s: float = 1.2  # voice-activated: end of an utterance
    min_speech_ms: int = 300
    no_speech_timeout_s: float = 10.0  # hands-free: give up if nothing is said (0 = never)
    max_recording_s: float = 300.0
    pre_roll_ms: int = 300
    # Transcription
    engine: str = "local"
    language: str = "tr"  # tr = Turkish + English (recommended), auto, en
    local_model: str = "large-v3-turbo-q5_0"
    whisper_server_path: str = ""  # empty = find whisper-server on PATH / Homebrew
    threads: int = 4
    repair_gaps: bool = True  # re-transcribe speech Whisper skipped (code-switch drops)
    prompt_tr: str = DEFAULT_PROMPT_TR
    prompt_en: str = DEFAULT_PROMPT_EN
    vocabulary: tuple[str, ...] = DEFAULT_VOCABULARY
    replacements: tuple[tuple[str, str], ...] = ()
    cloud_base_url: str = "https://api.openai.com/v1"
    cloud_model: str = "gpt-transcribe"
    cloud_api_key_env: str = "OPENAI_API_KEY"
    # Output
    insert_method: str = "paste"
    paste_shortcut: str = "ctrl_v"  # Linux: terminals need ctrl_shift_v
    paste_guard: bool = True  # app changed while transcribing → copy instead of paste
    restore_clipboard: bool = True
    restore_delay_ms: int = 800
    trailing_space: bool = True
    auto_enter: bool = False
    submit_phrases: tuple[str, ...] = ()  # e.g. ["gönder", "send it"] → press Enter
    # Feedback
    sounds: bool = True
    show_hud: bool = True
    # Diagnostics
    keep_last_recording: bool = False
    log_level: str = "INFO"


Validator = Callable[[Any], Any]


def _choice(*options: str) -> Validator:
    def validate(value: Any) -> str:
        if value not in options:
            raise ValueError(f"must be one of: {', '.join(options)}")
        return value

    return validate


def _number(lo: float, hi: float, integer: bool = False) -> Validator:
    def validate(value: Any) -> float | int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("must be a number")
        if not math.isfinite(value) or not lo <= value <= hi:
            raise ValueError(f"must be between {lo} and {hi}")
        if integer:
            if float(value) != int(value):
                raise ValueError("must be a whole number")
            return int(value)
        return float(value)

    return validate


def _boolean(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("must be true or false")
    return value


def _text(max_len: int, pattern: str | None = None) -> Validator:
    regex = re.compile(pattern) if pattern else None

    def validate(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("must be text")
        if len(value) > max_len:
            raise ValueError(f"must be at most {max_len} characters")
        if regex and not regex.fullmatch(value):
            raise ValueError("has an invalid format")
        return value

    return validate


def _text_list(max_items: int, max_len: int) -> Validator:
    def validate(value: Any) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("must be a list of text")
        if len(value) > max_items:
            raise ValueError(f"must have at most {max_items} entries")
        items = []
        for item in value:
            if not isinstance(item, str) or not item.strip() or len(item) > max_len:
                raise ValueError(f"entries must be non-empty text up to {max_len} characters")
            items.append(item.strip())
        return tuple(items)

    return validate


def _replacements(value: Any) -> tuple[tuple[str, str], ...]:
    if isinstance(value, dict):
        pairs = list(value.items())
    elif isinstance(value, (list, tuple)):
        pairs = [tuple(p) if isinstance(p, (list, tuple)) else (p,) for p in value]
    else:
        raise ValueError('must be an object like {"spoken": "written"}')
    if len(pairs) > 200:
        raise ValueError("must have at most 200 entries")
    result = []
    for pair in pairs:
        if len(pair) != 2:
            raise ValueError("entries must be [spoken, written] pairs")
        spoken, written = pair
        if not isinstance(spoken, str) or not spoken.strip() or len(spoken) > 100:
            raise ValueError("spoken text must be non-empty, up to 100 characters")
        if not isinstance(written, str) or len(written) > 500:
            raise ValueError("written text must be up to 500 characters")
        result.append((spoken.strip(), written))
    return tuple(result)


def _url(value: Any) -> str:
    text = _text(300)(value).rstrip("/")
    local = re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", text)
    if not (text.startswith("https://") or local):
        raise ValueError("must start with https:// (plain http only for localhost)")
    return text


def _model_key(value: Any) -> str:
    from .models import CATALOG  # lazy: keeps config importable on its own

    if value not in CATALOG:
        raise ValueError(f"must be one of: {', '.join(CATALOG)}")
    return value


def _executable_path(value: Any) -> str:
    text = _text(500)(value)
    if text and not text.startswith("/"):
        raise ValueError("must be an absolute path or empty")
    return text


SPECS: dict[str, Validator] = {
    "mode": _choice(*MODES),
    "hotkey": _choice(*HOTKEYS),
    "hold_threshold_ms": _number(50, 2000, integer=True),
    "double_tap_ms": _number(100, 1000, integer=True),
    "esc_cancels": _boolean,
    "input_device": _text(100),
    "mic_linger_s": _number(0, 600),
    "keep_mic_open": _boolean,
    "auto_threshold": _boolean,
    "noise_margin_db": _number(3, 40),
    "level_threshold_db": _number(-90, -5),
    "silence_stop_s": _number(0, 30),
    "voice_silence_stop_s": _number(0.3, 10),
    "min_speech_ms": _number(50, 5000, integer=True),
    "no_speech_timeout_s": _number(0, 120),
    "max_recording_s": _number(5, 1800),
    "pre_roll_ms": _number(0, 2000, integer=True),
    "engine": _choice(*ENGINES),
    "language": _choice(*LANGUAGES),
    "local_model": _model_key,
    "whisper_server_path": _executable_path,
    "threads": _number(1, 16, integer=True),
    "repair_gaps": _boolean,
    "prompt_tr": _text(1000),
    "prompt_en": _text(1000),
    "vocabulary": _text_list(100, 60),
    "replacements": _replacements,
    "cloud_base_url": _url,
    "cloud_model": _text(100, r"[A-Za-z0-9._:/-]+"),
    "cloud_api_key_env": _text(64, r"[A-Z_][A-Z0-9_]*"),
    "insert_method": _choice(*INSERT_METHODS),
    "paste_shortcut": _choice(*PASTE_SHORTCUTS),
    "paste_guard": _boolean,
    "restore_clipboard": _boolean,
    "restore_delay_ms": _number(50, 5000, integer=True),
    "trailing_space": _boolean,
    "auto_enter": _boolean,
    "submit_phrases": _text_list(20, 40),
    "sounds": _boolean,
    "show_hud": _boolean,
    "keep_last_recording": _boolean,
    "log_level": _choice(*LOG_LEVELS),
}


def settings_from_dict(raw: dict[str, Any]) -> tuple[Settings, list[str]]:
    defaults = Settings()
    values: dict[str, Any] = {}
    warnings: list[str] = []
    for key, value in raw.items():
        if key.startswith("_"):  # "_comment"-style keys are allowed
            continue
        spec = SPECS.get(key)
        if spec is None:
            warnings.append(f"Unknown setting '{key}' ignored.")
            continue
        try:
            values[key] = spec(value)
        except ValueError as exc:
            warnings.append(f"Setting '{key}' {exc}; using default {getattr(defaults, key)!r}.")
    return replace(defaults, **values), warnings


def settings_to_dict(settings: Settings) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for f in fields(settings):
        value = getattr(settings, f.name)
        if f.name == "replacements":
            value = {spoken: written for spoken, written in value}
        elif isinstance(value, tuple):
            value = list(value)
        data[f.name] = value
    return data


def load_settings(path: Path | None = None) -> tuple[Settings, list[str]]:
    path = path or paths.config_path()
    if not path.exists():
        return Settings(), []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return Settings(), [f"Could not read {path.name} ({exc}); using defaults."]
    if not isinstance(raw, dict):
        return Settings(), [f"{path.name} must contain a JSON object; using defaults."]
    return settings_from_dict(raw)


def save_settings(settings: Settings, path: Path | None = None) -> None:
    """Write atomically so a crash never leaves a half-written config."""
    path = path or paths.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(settings_to_dict(settings), indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def updated(settings: Settings, **changes: Any) -> Settings:
    """Return a copy with validated changes. Raises ValueError on bad input."""
    validated = {}
    for key, value in changes.items():
        spec = SPECS.get(key)
        if spec is None:
            raise ValueError(f"Unknown setting '{key}'")
        validated[key] = spec(value)
    return replace(settings, **validated)
