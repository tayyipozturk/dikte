"""Sound-level voice activity detection.

Levels are RMS in dBFS per 32 ms frame. The threshold is either fixed or
follows the room (noise floor + margin, never below the fixed threshold).
Speech is recognised by *sustained* level: most frames of a short window must
be above the threshold, which ignores keyboard clicks and other transients.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 16000
FRAME_SAMPLES = 512
FRAME_S = FRAME_SAMPLES / SAMPLE_RATE
SILENCE_DB = -100.0


def _db(amplitude: float) -> float:
    if amplitude <= 1e-10:
        return SILENCE_DB
    return max(SILENCE_DB, 20.0 * math.log10(amplitude))


def level_db(frame: np.ndarray) -> float:
    if frame.size == 0:
        return SILENCE_DB
    return _db(float(np.sqrt(np.mean(np.square(frame, dtype=np.float64)))))


def peak_db(audio: np.ndarray) -> float:
    if audio.size == 0:
        return SILENCE_DB
    return _db(float(np.max(np.abs(audio))))


class NoiseFloor:
    """Tracks the quiet level of the room: falls quickly, rises slowly."""

    def __init__(self, initial_db: float = -60.0, rise_db_per_s: float = 0.5) -> None:
        self.value = initial_db
        self._rise = rise_db_per_s
        self._frames = 0

    def update(self, level: float) -> float:
        self._frames += 1
        if level < self.value:
            self.value += (level - self.value) * 0.3
        else:
            # Adapt fast during the first second so a noisy room is learned quickly.
            rise = 20.0 if self._frames < int(1.0 / FRAME_S) else self._rise
            self.value = min(level, self.value + rise * FRAME_S)
        return self.value


@dataclass(frozen=True)
class GateParams:
    auto: bool = True
    margin_db: float = 10.0
    threshold_db: float = -45.0
    hysteresis_db: float = 3.0


class LevelGate:
    """Per-frame level, noise floor and the resulting speech threshold."""

    def __init__(self, params: GateParams) -> None:
        self.params = params
        self.floor = NoiseFloor()
        self.level = SILENCE_DB

    @property
    def threshold(self) -> float:
        if not self.params.auto:
            return self.params.threshold_db
        return max(self.floor.value + self.params.margin_db, self.params.threshold_db)

    def update(self, frame: np.ndarray) -> float:
        self.level = level_db(frame)
        self.floor.update(self.level)
        return self.level

    def is_voiced(self, level: float) -> bool:
        return level >= self.threshold

    def is_quiet(self, level: float) -> bool:
        return level < self.threshold - self.params.hysteresis_db


@dataclass(frozen=True)
class VoiceParams:
    onset_s: float = 0.25  # window used to confirm speech
    onset_ratio: float = 0.6  # fraction of voiced frames needed in that window
    silence_stop_s: float = 1.2
    min_speech_s: float = 0.3
    pre_roll_s: float = 0.3
    max_utterance_s: float = 60.0
    tail_s: float = 0.2  # silence kept after the last word


def _frames(seconds: float) -> int:
    return max(1, int(round(seconds / FRAME_S)))


class OnsetTracker:
    """Confirms speech when enough recent frames are voiced."""

    def __init__(self, onset_s: float, ratio: float) -> None:
        self._flags: deque[bool] = deque(maxlen=_frames(onset_s))
        self._needed = math.ceil(ratio * self._flags.maxlen)

    def push(self, voiced: bool) -> bool:
        self._flags.append(voiced)
        return len(self._flags) == self._flags.maxlen and sum(self._flags) >= self._needed

    def voiced_count(self) -> int:
        return sum(self._flags)

    def clear(self) -> None:
        self._flags.clear()


class UtteranceDetector:
    """Voice-activated recording. feed() returns events:
    ("start", None), ("end", audio) or ("discard", None)."""

    def __init__(self, gate: LevelGate, params: VoiceParams) -> None:
        self.gate = gate
        self.params = params
        self._onset = OnsetTracker(params.onset_s, params.onset_ratio)
        self._ring: deque[np.ndarray] = deque(maxlen=_frames(params.pre_roll_s) + _frames(params.onset_s))
        self._frames: list[np.ndarray] | None = None
        self._voiced = 0
        self._silence = 0

    @property
    def speaking(self) -> bool:
        return self._frames is not None

    def reset(self) -> None:
        self._onset.clear()
        self._ring.clear()
        self._frames = None

    def take_frames(self) -> list[np.ndarray]:
        """Hand over the current utterance (e.g. to a push-to-talk recording) and reset."""
        frames = list(self._frames or [])
        self.reset()
        return frames

    def feed(self, frame: np.ndarray) -> list[tuple[str, np.ndarray | None]]:
        level = self.gate.update(frame)
        if self._frames is None:
            self._ring.append(frame)
            if self._onset.push(self.gate.is_voiced(level)):
                self._frames = list(self._ring)
                self._voiced = self._onset.voiced_count()
                self._silence = 0
                self._ring.clear()
                self._onset.clear()
                return [("start", None)]
            return []

        self._frames.append(frame)
        if self.gate.is_quiet(level):
            self._silence += 1
        else:
            self._silence = 0
            if self.gate.is_voiced(level):
                self._voiced += 1

        if self._silence * FRAME_S >= self.params.silence_stop_s:
            return [self._finish(trim_silence=True)]
        if len(self._frames) * FRAME_S >= self.params.max_utterance_s:
            if self._silence:  # already in a pause: end here instead of splitting
                return [self._finish(trim_silence=True)]
            event = self._finish(trim_silence=False)
            self._frames, self._voiced, self._silence = [], 0, 0  # keep listening mid-speech
            return [event, ("start", None)]
        return []

    def _finish(self, trim_silence: bool) -> tuple[str, np.ndarray | None]:
        frames = self._frames or []
        if trim_silence:
            keep = len(frames) - self._silence + _frames(self.params.tail_s)
            frames = frames[: max(0, keep)]
        voiced_s = self._voiced * FRAME_S
        self._frames = None
        self._onset.clear()
        if voiced_s < self.params.min_speech_s or not frames:
            return ("discard", None)
        return ("end", np.concatenate(frames))


class SilenceWatcher:
    """Auto-stop for hands-free recordings: "stop" after silence that follows
    speech, "timeout" if no speech starts in time. 0 disables either."""

    def __init__(self, gate: LevelGate, silence_stop_s: float, no_speech_timeout_s: float,
                 onset_s: float = 0.25, onset_ratio: float = 0.6) -> None:
        self.gate = gate
        self._silence_stop_s = silence_stop_s
        self._timeout_s = no_speech_timeout_s
        self._onset = OnsetTracker(onset_s, onset_ratio)
        self.speech_seen = False
        self._silence = 0
        self._elapsed = 0

    def feed(self, frame: np.ndarray) -> str | None:
        level = self.gate.update(frame)
        self._elapsed += 1
        if not self.speech_seen:
            self.speech_seen = self._onset.push(self.gate.is_voiced(level))
            if not self.speech_seen and self._timeout_s and self._elapsed * FRAME_S >= self._timeout_s:
                return "timeout"
            return None
        self._silence = self._silence + 1 if self.gate.is_quiet(level) else 0
        if self._silence_stop_s and self._silence * FRAME_S >= self._silence_stop_s:
            return "stop"
        return None


def speech_regions(audio: np.ndarray, min_region_s: float = 0.25,
                   merge_gap_s: float = 0.3) -> list[tuple[float, float]]:
    """Rough speech regions (seconds) of a finished recording, by energy."""
    count = len(audio) // FRAME_SAMPLES
    if count == 0:
        return []
    frames = audio[: count * FRAME_SAMPLES].reshape(count, FRAME_SAMPLES).astype(np.float64)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    levels = 20.0 * np.log10(np.maximum(rms, 1e-10))
    threshold = max(float(np.percentile(levels, 10)) + 10.0, float(levels.max()) - 35.0, -60.0)
    voiced = levels > threshold

    regions: list[list[int]] = []
    for i, is_voiced in enumerate(voiced):
        if not is_voiced:
            continue
        if regions and (i - regions[-1][1]) * FRAME_S <= merge_gap_s:
            regions[-1][1] = i + 1
        else:
            regions.append([i, i + 1])
    return [
        (start * FRAME_S, end * FRAME_S)
        for start, end in regions
        if (end - start) * FRAME_S >= min_region_s
    ]
