"""Hotkey gesture recognition. Pure logic: fed key events with timestamps,
returns the actions the controller should take.

Modes
  hold   : hold to talk; double-tap starts a hands-free recording.
  toggle : tap starts a hands-free recording; holding still works as push-to-talk.
  voice  : tap pauses/resumes voice-activated listening; holding is push-to-talk.
In a hands-free recording a tap stops it. Esc cancels any recording.

Audio capture starts on key-down (PREPARE) so the first syllable is never lost,
but the recording only becomes "real" (sound + HUD) once the key has been held
for hold_s (BEGIN). Another key pressed soon after the hotkey means it was a
shortcut such as Right-Cmd+C, so the capture is cancelled.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class Action(Enum):
    PREPARE = auto()  # key down: start capturing audio (no UI yet)
    BEGIN = auto()  # held long enough: this is a recording
    FINISH = auto()  # stop and transcribe
    CANCEL = auto()  # discard captured audio
    HANDS_FREE = auto()  # recording continues without holding the key
    TOGGLE_LISTEN = auto()  # voice mode: pause/resume listening


class _State(Enum):
    IDLE = auto()
    PRESSED = auto()  # down, shorter than hold_s so far
    HOLDING = auto()  # push-to-talk in progress
    COMBO = auto()  # part of a shortcut: ignore until release
    HANDS_FREE = auto()
    HF_PRESSED = auto()  # key down during a hands-free recording
    HF_COMBO = auto()  # shortcut pressed during a hands-free recording


@dataclass(frozen=True)
class GestureConfig:
    mode: str = "hold"
    hold_s: float = 0.2
    double_tap_s: float = 0.35
    combo_window_s: float = 1.0


class HotkeyGesture:
    def __init__(self, config: GestureConfig) -> None:
        self.config = config
        self._state = _State.IDLE
        self._down_at = 0.0
        self._last_tap_up: float | None = None

    @property
    def hands_free(self) -> bool:
        return self._state in (_State.HANDS_FREE, _State.HF_PRESSED, _State.HF_COMBO)

    def reconfigure(self, config: GestureConfig) -> None:
        self.config = config
        self._state = _State.IDLE
        self._last_tap_up = None

    def key_down(self, t: float) -> list[Action]:
        if self._state is _State.IDLE:
            self._state, self._down_at = _State.PRESSED, t
            return [Action.PREPARE]
        if self._state is _State.HANDS_FREE:
            self._state, self._down_at = _State.HF_PRESSED, t
        return []

    def key_up(self, t: float) -> list[Action]:
        state = self._state
        if state is _State.PRESSED:
            return self._tap(t)
        if state is _State.HOLDING:
            self._state = _State.IDLE
            return [Action.FINISH]
        if state is _State.COMBO:
            self._state = _State.IDLE
        elif state is _State.HF_PRESSED:
            self._state = _State.IDLE
            return [Action.FINISH]
        elif state is _State.HF_COMBO:
            self._state = _State.HANDS_FREE
        return []

    def other_key(self, t: float) -> list[Action]:
        state = self._state
        in_window = t - self._down_at <= self.config.combo_window_s
        if state is _State.PRESSED or (state is _State.HOLDING and in_window):
            self._state = _State.COMBO
            self._last_tap_up = None
            return [Action.CANCEL]
        if state is _State.HF_PRESSED:
            self._state = _State.HF_COMBO
        return []

    def escape(self, t: float) -> list[Action]:
        state = self._state
        if state in (_State.PRESSED, _State.HOLDING, _State.HF_PRESSED, _State.HF_COMBO):
            self._state = _State.COMBO  # wait for the hotkey release
            return [Action.CANCEL]
        if state is _State.HANDS_FREE:
            self._state = _State.IDLE
            return [Action.CANCEL]
        return []

    def tick(self, t: float) -> list[Action]:
        if self._state is _State.PRESSED and t - self._down_at >= self.config.hold_s:
            self._state = _State.HOLDING
            return [Action.BEGIN]
        return []

    def end_hands_free(self) -> None:
        """The recording stopped for another reason (silence, menu, max length)."""
        if self._state is _State.HANDS_FREE:
            self._state = _State.IDLE
        elif self._state in (_State.HF_PRESSED, _State.HF_COMBO):
            self._state = _State.COMBO

    def _tap(self, t: float) -> list[Action]:
        mode = self.config.mode
        if mode == "toggle":
            self._state = _State.HANDS_FREE
            return [Action.HANDS_FREE]
        if mode == "voice":
            self._state = _State.IDLE
            return [Action.CANCEL, Action.TOGGLE_LISTEN]
        last = self._last_tap_up
        if last is not None and self._down_at - last <= self.config.double_tap_s:
            self._last_tap_up = None
            self._state = _State.HANDS_FREE
            return [Action.HANDS_FREE]
        self._last_tap_up = t
        self._state = _State.IDLE
        return [Action.CANCEL]
