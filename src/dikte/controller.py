"""Turns hotkey gestures and audio frames into recordings and transcription jobs.

All state is owned by one thread. The audio callback, hotkey tap, transcriber
and menu only put events on a queue, so the state machine needs no locks.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Protocol

import numpy as np

from .config import Settings
from .gesture import Action, GestureConfig, HotkeyGesture
from .levels import (
    FRAME_S,
    SILENCE_DB,
    GateParams,
    LevelGate,
    SilenceWatcher,
    UtteranceDetector,
    VoiceParams,
    level_db,
    peak_db,
)
from .transcriber import Job, JobResult

log = logging.getLogger(__name__)

RELEASE_TAIL_S = 0.15  # keep recording briefly after the key is released
MUTED_PEAK_DB = -90.0  # digital silence: mic muted or permission missing
QUIET_PEAK_DB = -50.0
STALL_S = 2.0
LEVEL_REFRESH_S = 0.1
CALIBRATION_S = 2.5
MESSAGE_S = 6.0
MIC_RETRY_S = 5.0
HISTORY_SIZE = 10
VOICE_MAX_UTTERANCE_S = 60.0


@dataclass(frozen=True)
class Status:
    phase: str = "starting"  # starting | idle | listening | recording | transcribing | paused | error
    message: str = ""
    device: str = ""
    level_db: float = SILENCE_DB
    threshold_db: float = -45.0
    floor_db: float = SILENCE_DB
    recording_s: float = 0.0
    hands_free: bool = False
    pending: int = 0


class Ui(Protocol):
    def status_changed(self, status: Status) -> None: ...
    def play(self, sound: str) -> None: ...
    def notify(self, message: str) -> None: ...
    def history_changed(self, items: tuple[str, ...]) -> None: ...
    def calibrated(self, threshold_db: float) -> None: ...


class Audio(Protocol):
    @property
    def is_open(self) -> bool: ...
    @property
    def device_name(self) -> str: ...
    def open(self, preferred: str) -> str: ...
    def close(self) -> None: ...
    def rescan(self) -> None: ...


class Worker(Protocol):
    def configure(self, settings: Settings) -> None: ...
    def submit(self, job: Job) -> None: ...
    def insert_text(self, text: str, settings: Settings) -> None: ...


def _gesture_config(s: Settings) -> GestureConfig:
    return GestureConfig(mode=s.mode, hold_s=s.hold_threshold_ms / 1000, double_tap_s=s.double_tap_ms / 1000)


def _gate_params(s: Settings) -> GateParams:
    return GateParams(auto=s.auto_threshold, margin_db=s.noise_margin_db, threshold_db=s.level_threshold_db)


def _voice_params(s: Settings) -> VoiceParams:
    return VoiceParams(silence_stop_s=s.voice_silence_stop_s, min_speech_s=s.min_speech_ms / 1000,
                       pre_roll_s=s.pre_roll_ms / 1000,
                       max_utterance_s=min(VOICE_MAX_UTTERANCE_S, s.max_recording_s))


class Controller:
    def __init__(self, settings: Settings, audio: Audio, worker: Worker,
                 frontmost_pid: Callable[[], int | None], ui: Ui,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._events: queue.Queue[tuple] = queue.Queue()
        self._settings = settings
        self._audio = audio
        self._worker = worker
        self._frontmost_pid = frontmost_pid
        self._ui = ui
        self._clock = clock
        self._gesture = HotkeyGesture(_gesture_config(settings))
        self._gate = LevelGate(_gate_params(settings))
        self._detector = UtteranceDetector(self._gate, _voice_params(settings))
        self._ring: deque[np.ndarray] = deque(maxlen=self._pre_roll_frames(settings))
        self._rec: list[np.ndarray] | None = None
        self._ui_recording = False
        self._watcher: SilenceWatcher | None = None
        self._finish_at: float | None = None
        self._target_pid: int | None = None
        self._listening = settings.mode == "voice"
        self._paused = False
        self._close_at: float | None = None
        self._last_frame_at = 0.0
        self._devices_dirty = False
        self._policy_applied = False
        self._retry_at = 0.0
        self._pending = 0
        self._engine_ready = False
        self._engine_error = ""
        self._mic_error = ""
        self._message = ""
        self._message_until = 0.0
        self._calibration: list[float] | None = None
        self._calibrate_until = 0.0
        self._history: deque[str] = deque(maxlen=HISTORY_SIZE)
        self._last_status: Status | None = None
        self._last_status_at = 0.0
        self._running = False
        self._thread: threading.Thread | None = None

    # thread-safe API ---------------------------------------------------------

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, name="dikte-controller", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._events.put(("stop",))
        if self._thread is not None:
            self._thread.join(timeout)

    def push_frame(self, frame: np.ndarray) -> None:
        self._events.put(("frame", frame))

    def hotkey(self, kind: str, t: float) -> None:
        self._events.put(("key", kind, t))

    def command(self, name: str, arg: object = None) -> None:
        self._events.put(("cmd", name, arg))

    def job_finished(self, result: JobResult) -> None:
        self._events.put(("result", result))

    def engine_state(self, ready: bool, message: str) -> None:
        self._events.put(("engine", ready, message))

    # event loop ----------------------------------------------------------------

    def _run(self) -> None:
        while self._running:
            try:
                event = self._events.get(timeout=0.05)
            except queue.Empty:
                event = None
            now = self._clock()
            try:
                if event is not None:
                    self._dispatch(event, now)
                self._periodic(now)
            except Exception:  # noqa: BLE001 - the loop must never die
                log.exception("Controller error while handling %s", event[0] if event else "tick")
        self._audio.close()

    def run_pending(self) -> None:
        """Process queued events synchronously (tests)."""
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            now = self._clock()
            self._dispatch(event, now)
            self._periodic(now)
        self._periodic(self._clock())

    def _dispatch(self, event: tuple, now: float) -> None:
        kind = event[0]
        if kind == "frame":
            self._on_frame(event[1], now)
        elif kind == "key":
            self._on_key(event[1], event[2])
        elif kind == "cmd":
            self._on_command(event[1], event[2], now)
        elif kind == "result":
            self._on_result(event[1], now)
        elif kind == "engine":
            self._engine_ready, self._engine_error = event[1], ("" if event[1] or "Loading" in event[2] else event[2])
            if self._engine_error:
                self._ui.notify(self._engine_error)
            self._push_status(now, force=True)
        elif kind == "stop":
            self._running = False

    def _periodic(self, now: float) -> None:
        if not self._policy_applied:  # first tick: open the mic now if the mode needs it
            self._policy_applied = True
            self._apply_stream_policy(now)
        for action in self._gesture.tick(now):
            self._do(action, now)
        if self._finish_at is not None and now >= self._finish_at:
            self._finish_now(now)
        if self._calibration is not None and now >= self._calibrate_until:
            self._finish_calibration(now)
        if self._close_at is not None and now >= self._close_at:
            self._close_at = None
            if not self._needs_stream():
                self._audio.close()
        if self._audio.is_open and now - self._last_frame_at > STALL_S:
            self._recover_stream(now, "audio stopped arriving")
        elif self._mic_error and not self._audio.is_open and self._needs_stream() and now >= self._retry_at:
            self._retry_at = now + MIC_RETRY_S  # e.g. voice mode waiting for the mic to be plugged in
            self._ensure_stream(now, background=True)
        if self._devices_dirty and self._rec is None:
            self._devices_dirty = False
            self._refresh_devices(now)
        self._push_status(now)

    # hotkey --------------------------------------------------------------------

    def _on_key(self, kind: str, t: float) -> None:
        if self._paused:
            return
        if kind == "escape" and not self._settings.esc_cancels:
            return
        if kind == "escape" and self._detector.speaking:  # voice mode: drop the current utterance
            self._detector.reset()
            self._ui.play("cancel")
            self._set_message("Recording cancelled", self._clock())
            self._push_status(self._clock(), force=True)
        handler = {"down": self._gesture.key_down, "up": self._gesture.key_up,
                   "other": self._gesture.other_key, "escape": self._gesture.escape}.get(kind)
        if handler is None:
            return
        for action in handler(t):
            self._do(action, t)

    def _do(self, action: Action, t: float) -> None:
        now = self._clock()
        if action is Action.PREPARE:
            self._prepare(now)
        elif action is Action.BEGIN:
            self._begin_ui(now)
        elif action is Action.HANDS_FREE:
            if self._rec is None:
                self._prepare(now)
            if self._rec is None:
                self._gesture.end_hands_free()
                return
            s = self._settings
            self._watcher = SilenceWatcher(self._gate, s.silence_stop_s, s.no_speech_timeout_s)
            self._begin_ui(now)
        elif action is Action.FINISH:
            if self._rec is not None and self._finish_at is None:
                self._finish_at = now + RELEASE_TAIL_S
        elif action is Action.CANCEL:
            self._cancel(now)
        elif action is Action.TOGGLE_LISTEN:
            self._listening = not self._listening
            self._detector.reset()
            self._ui.play("listen" if self._listening else "cancel")
            self._set_message("Listening" if self._listening else "Listening paused", now)
            self._apply_stream_policy(now)

    # recording -------------------------------------------------------------------

    def _prepare(self, now: float) -> None:
        if self._finish_at is not None:
            self._finish_now(now)  # a new press arrived during the release tail
        if not self._ensure_stream(now):
            return
        # A manual recording takes over from voice detection, keeping what it heard so far.
        self._rec = self._detector.take_frames() if self._detector.speaking else list(self._ring)
        self._detector.reset()
        self._ring.clear()  # pre-roll must never reach back past the previous recording
        self._watcher = None
        self._ui_recording = False

    def _begin_ui(self, now: float) -> None:
        if self._rec is None or self._ui_recording:
            return
        self._ui_recording = True
        self._target_pid = self._frontmost_pid() if self._settings.paste_guard else None
        self._ui.play("start")
        self._push_status(now, force=True)

    def _cancel(self, now: float) -> None:
        if self._rec is None:
            return
        was_visible = self._ui_recording
        self._reset_recording(now)
        if was_visible:
            self._ui.play("cancel")
            self._set_message("Recording cancelled", now)

    def _reset_recording(self, now: float) -> None:
        self._rec = None
        self._ui_recording = False
        self._watcher = None
        self._finish_at = None
        self._gesture.end_hands_free()
        self._schedule_close(now)
        self._push_status(now, force=True)

    def _finish_now(self, now: float) -> None:
        frames = self._rec or []
        was_visible = self._ui_recording
        target = self._target_pid
        self._reset_recording(now)
        if not was_visible:
            return
        self._ui.play("stop")
        audio = np.concatenate(frames) if frames else np.empty(0, np.float32)
        peak = peak_db(audio)
        if peak < MUTED_PEAK_DB:
            self._ui.play("error")
            self._set_message("Mic is silent: is it muted?", now)
            self._ui.notify("The microphone sent pure silence. Is it muted? Check its mute button or light.")
            return
        self._submit(audio, target, peak, now)

    def _submit(self, audio: np.ndarray, target: int | None, peak: float, now: float) -> None:
        self._pending += 1
        self._worker.submit(Job(audio=audio, settings=self._settings, target_pid=target, peak_db=peak))
        self._push_status(now, force=True)

    # audio frames ----------------------------------------------------------------

    def _on_frame(self, frame: np.ndarray, now: float) -> None:
        self._last_frame_at = now
        if self._calibration is not None:
            self._calibration.append(level_db(frame))

        if self._rec is not None:
            self._rec.append(frame)
            verdict = self._watcher.feed(frame) if self._watcher else None
            if self._watcher is None:
                self._gate.update(frame)
            if verdict == "stop":
                self._finish_now(now)
            elif verdict == "timeout":
                self._cancel(now)
                self._set_message("No speech heard; recording stopped", now)
            elif len(self._rec) * FRAME_S >= self._settings.max_recording_s:
                self._set_message("Maximum recording length reached", now)
                self._begin_ui(now)  # may not be visible yet (speech handed over from voice mode)
                self._finish_now(now)
            return

        self._ring.append(frame)
        if self._voice_active():
            for event, audio in self._detector.feed(frame):
                if event == "start":
                    self._target_pid = self._frontmost_pid() if self._settings.paste_guard else None
                    self._push_status(now, force=True)
                elif event == "end" and audio is not None:
                    self._submit(audio, self._target_pid, peak_db(audio), now)
                else:
                    self._push_status(now, force=True)
        else:
            self._gate.update(frame)

    def _voice_active(self) -> bool:
        return self._settings.mode == "voice" and self._listening and not self._paused

    # microphone stream -------------------------------------------------------------

    def _needs_stream(self) -> bool:
        return (self._rec is not None or self._calibration is not None
                or self._voice_active() or (self._settings.keep_mic_open and not self._paused))

    def _ensure_stream(self, now: float, background: bool = False) -> bool:
        """background=True for automatic retries: report a failure only once,
        while every key press that fails still gets a beep and a notice."""
        self._close_at = None
        if self._audio.is_open:
            return True
        try:
            self._audio.open(self._settings.input_device)
        except Exception as exc:  # noqa: BLE001 - AudioError or PortAudio failures
            if not background or str(exc) != self._mic_error:
                self._ui.notify(str(exc))
                self._ui.play("error")
            self._mic_error = str(exc)
            self._push_status(now, force=True)
            return False
        self._mic_error = ""
        self._last_frame_at = self._clock()  # opening can be slow; start the stall timer after it
        self._ring.clear()
        return True

    def _schedule_close(self, now: float) -> None:
        if not self._needs_stream():
            self._close_at = now + self._settings.mic_linger_s

    def _apply_stream_policy(self, now: float) -> None:
        if self._needs_stream():
            self._ensure_stream(now)
        elif self._audio.is_open:
            self._schedule_close(now)

    def _recover_stream(self, now: float, reason: str) -> None:
        log.warning("Microphone problem (%s); reopening", reason)
        if self._rec is not None:
            self._finish_now(now)  # keep what was recorded so far
        self._audio.close()
        self._audio.rescan()
        self._detector.reset()
        if self._needs_stream():
            self._ensure_stream(now)

    def _refresh_devices(self, now: float) -> None:
        # Reopen only if the mode needs it; a merely lingering mic just closes (the
        # next recording opens the right device) instead of staying open for good.
        self._audio.close()
        self._audio.rescan()
        if self._needs_stream():
            self._ensure_stream(now)

    # commands ------------------------------------------------------------------------

    def _on_command(self, name: str, arg: object, now: float) -> None:
        if name == "settings" and isinstance(arg, Settings):
            self._apply_settings(arg, now)
        elif name == "pause":
            self._paused = True
            self._cancel(now)
            self._detector.reset()
            self._gesture.reconfigure(self._gesture.config)  # forget a key held while pausing
            self._apply_stream_policy(now)
        elif name == "resume":
            self._paused = False
            self._apply_stream_policy(now)
        elif name == "cancel":
            self._cancel(now)
            self._detector.reset()
        elif name == "toggle":  # `dikte toggle`, e.g. from a desktop keyboard shortcut
            if self._rec is not None:
                self._finish_now(now)
            else:
                self._do(Action.PREPARE, now)
                self._do(Action.HANDS_FREE, now)
        elif name == "calibrate":
            if self._ensure_stream(now):
                self._calibration = []
                self._calibrate_until = now + CALIBRATION_S
                self._set_message("Calibrating: stay quiet for 2 seconds…", now)
        elif name == "devices_changed":
            self._devices_dirty = True
        elif name == "insert_text" and isinstance(arg, str):
            self._worker.insert_text(arg, self._settings)
        self._push_status(now, force=True)

    def _apply_settings(self, new: Settings, now: float) -> None:
        old, self._settings = self._settings, new
        if _gesture_config(old) != _gesture_config(new):
            self._cancel(now)
            self._gesture.reconfigure(_gesture_config(new))
        if new.mode != old.mode:
            self._listening = new.mode == "voice"
            self._detector.reset()  # an utterance in progress must not stay "recording"
        self._gate.params = _gate_params(new)
        if _voice_params(old) != _voice_params(new):
            self._detector = UtteranceDetector(self._gate, _voice_params(new))
            self._ring = deque(self._ring, maxlen=self._pre_roll_frames(new))
        if new.input_device != old.input_device:
            self._mic_error = ""
            if self._rec is None:
                self._audio.close()
            else:
                self._devices_dirty = True  # switch as soon as this recording ends
        self._worker.configure(new)
        self._apply_stream_policy(now)

    def _finish_calibration(self, now: float) -> None:
        levels, self._calibration = self._calibration or [], None
        self._schedule_close(now)
        if len(levels) < 10:
            self._set_message("Calibration failed: no audio from the microphone", now)
            return
        floor = float(np.median(levels[5:]))
        threshold = round(min(-5.0, max(-90.0, floor + self._settings.noise_margin_db)), 1)
        self._set_message(f"Room noise {floor:.0f} dB → threshold {threshold:.0f} dB", now)
        self._ui.calibrated(threshold)

    # results & status ------------------------------------------------------------------

    def _on_result(self, result: JobResult, now: float) -> None:
        self._pending = max(0, self._pending - 1)
        if result.error:
            self._ui.play("error")
            self._ui.notify(result.error)
            self._set_message(result.error, now)
        elif result.copied_only and result.copy_reason == "no_permission":
            self._set_message("Accessibility permission missing: text copied to clipboard", now)
            self._ui.notify("Dikte needs Accessibility permission to paste. Your text is on the clipboard (⌘V).")
        elif result.copied_only:
            self._set_message("App changed while transcribing: text copied to clipboard", now)
            self._ui.notify("You switched apps, so the text was copied to the clipboard instead of pasted.")
        elif result.text:
            words = len(result.text.split())
            self._set_message(f"Inserted {words} word{'s' if words != 1 else ''} ({result.elapsed_s:.1f} s)", now)
        elif result.submitted:
            self._set_message("Sent", now)
        elif result.peak_db < QUIET_PEAK_DB:
            self._set_message("No speech heard: mic too quiet or muted?", now)
        else:
            self._set_message("No speech detected", now)
        if result.text:
            self._history.appendleft(result.text)
            self._ui.history_changed(tuple(self._history))
        self._push_status(now, force=True)

    def _set_message(self, message: str, now: float) -> None:
        self._message, self._message_until = message, now + MESSAGE_S

    def _phase(self) -> str:
        if self._paused:
            return "paused"
        if self._ui_recording or self._detector.speaking:
            return "recording"
        if self._pending:
            return "transcribing"
        if self._engine_error:
            return "error"
        if not self._engine_ready:
            return "starting"
        if self._mic_error and self._needs_stream():
            return "error"
        return "listening" if self._voice_active() else "idle"

    def _push_status(self, now: float, force: bool = False) -> None:
        message = self._message if now < self._message_until else (self._engine_error or self._mic_error)
        status = Status(
            phase=self._phase(),
            message=message,
            device=self._audio.device_name,
            level_db=round(self._gate.level, 1),
            threshold_db=round(self._gate.threshold, 1),
            floor_db=round(self._gate.floor.value, 1),
            recording_s=round(len(self._rec) * FRAME_S, 1) if self._ui_recording and self._rec else 0.0,
            hands_free=self._gesture.hands_free,
            pending=self._pending,
        )
        changed = self._last_status is None or status.phase != self._last_status.phase or status.message != self._last_status.message
        if force or changed or now - self._last_status_at >= LEVEL_REFRESH_S:
            if status != self._last_status:
                self._ui.status_changed(status)
            self._last_status, self._last_status_at = status, now

    @staticmethod
    def _pre_roll_frames(settings: Settings) -> int:
        return max(1, int(round(settings.pre_roll_ms / 1000 / FRAME_S)))
