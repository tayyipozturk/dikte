"""Microphone capture with sounddevice (PortAudio / CoreAudio).

Delivers 512-sample (32 ms) mono float32 frames at 16 kHz to a consumer that
is called on the audio thread and must return quickly.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np
import sounddevice as sd

from .levels import FRAME_SAMPLES, SAMPLE_RATE

log = logging.getLogger(__name__)

FrameConsumer = Callable[[np.ndarray], None]
_RESCAN_INTERVAL_S = 5.0


class AudioError(Exception):
    pass


@dataclass(frozen=True)
class InputDevice:
    index: int
    name: str
    sample_rate: float
    channels: int


def list_input_devices() -> list[InputDevice]:
    return [
        InputDevice(i, str(d["name"]), float(d["default_samplerate"]), int(d["max_input_channels"]))
        for i, d in enumerate(sd.query_devices())
        if d["max_input_channels"] > 0
    ]


def pick_device(devices: list[InputDevice], preferred: str) -> InputDevice | None:
    if preferred:
        wanted = preferred.casefold()
        match = next((d for d in devices if wanted in d.name.casefold()), None)
        if match:
            return match
    try:
        default_index = sd.default.device[0]
    except (sd.PortAudioError, TypeError, IndexError):
        default_index = -1
    return next((d for d in devices if d.index == default_index), devices[0] if devices else None)


def _close_stream(stream: sd.InputStream) -> None:
    try:
        stream.stop()
    except sd.PortAudioError as exc:
        log.warning("Stopping the microphone failed: %s", exc)
    finally:
        try:
            stream.close()
        except sd.PortAudioError as exc:
            log.warning("Closing the microphone failed: %s", exc)


class Rechunker:
    """Turns arbitrary block sizes into fixed-size frames."""

    def __init__(self, size: int = FRAME_SAMPLES) -> None:
        self._size = size
        self._pending = np.empty(0, np.float32)

    def push(self, block: np.ndarray) -> list[np.ndarray]:
        data = np.concatenate([self._pending, block]) if self._pending.size else block
        count = len(data) // self._size
        frames = [data[i * self._size:(i + 1) * self._size] for i in range(count)]
        self._pending = data[count * self._size:].copy()
        return frames


class Resampler:
    """Streaming low-pass + linear-interpolation resampler to 16 kHz.
    Only used if the device refuses to open at 16 kHz directly."""

    _TAPS = 63

    def __init__(self, rate_in: float) -> None:
        self._step = rate_in / SAMPLE_RATE
        cutoff = min(0.45, 0.45 / self._step)  # cycles per input sample, below both Nyquists
        n = np.arange(self._TAPS) - (self._TAPS - 1) / 2
        taps = np.sinc(2 * cutoff * n) * np.hamming(self._TAPS)
        self._taps = (taps / taps.sum()).astype(np.float32)
        self._history = np.zeros(self._TAPS - 1, np.float32)
        self._buffer = np.empty(0, np.float32)
        self._pos = 0.0

    def process(self, block: np.ndarray) -> np.ndarray:
        padded = np.concatenate([self._history, block])
        filtered = np.convolve(padded, self._taps, mode="valid").astype(np.float32)
        self._history = padded[-(self._TAPS - 1):]
        buffer = np.concatenate([self._buffer, filtered])
        if len(buffer) - 1 < self._pos:
            self._buffer = buffer
            return np.empty(0, np.float32)
        count = int(np.floor((len(buffer) - 1 - self._pos) / self._step)) + 1
        positions = self._pos + np.arange(count) * self._step
        out = np.interp(positions, np.arange(len(buffer)), buffer).astype(np.float32)
        next_pos = self._pos + count * self._step
        drop = min(int(np.floor(next_pos)), len(buffer))
        self._buffer = buffer[drop:]
        self._pos = next_pos - drop
        return out


class AudioInput:
    def __init__(self) -> None:
        self._stream: sd.InputStream | None = None
        self._device: InputDevice | None = None
        self._consumer: FrameConsumer | None = None
        self._rechunker = Rechunker()
        self._resampler: Resampler | None = None
        self._last_rescan = 0.0
        self._callback_errors = 0

    def set_consumer(self, consumer: FrameConsumer) -> None:
        self._consumer = consumer

    @property
    def is_open(self) -> bool:
        return self._stream is not None

    @property
    def device_name(self) -> str:
        return self._device.name if self._device else ""

    def open(self, preferred: str) -> str:
        if self._stream is not None and self._device is not None:
            return self._device.name
        device = pick_device(self._devices(preferred), preferred)
        if device is None:
            raise AudioError("No microphone found. Plug in your mic or pick another input.")
        self._stream = self._start(device)
        self._device = device
        log.info("Microphone open: %s", device.name)
        return device.name

    def close(self) -> None:
        stream, self._stream = self._stream, None
        self._device = None
        if stream is None:
            return
        _close_stream(stream)
        log.info("Microphone closed")

    def rescan(self) -> None:
        """Refresh PortAudio's device list (it is cached at start-up). Only while closed."""
        if self._stream is not None:
            return
        self._last_rescan = time.monotonic()
        try:
            sd._terminate()
            sd._initialize()
        except Exception:  # noqa: BLE001 - PortAudio can fail in many ways; keep running
            log.exception("Rescanning audio devices failed")

    def _devices(self, preferred: str) -> list[InputDevice]:
        devices = list_input_devices()
        missing = not devices or (preferred and not any(preferred.casefold() in d.name.casefold() for d in devices))
        if missing and time.monotonic() - self._last_rescan > _RESCAN_INTERVAL_S:
            self.rescan()
            devices = list_input_devices()
        return devices

    def _start(self, device: InputDevice) -> sd.InputStream:
        for rate in dict.fromkeys((SAMPLE_RATE, int(device.sample_rate))):
            self._resampler = None if rate == SAMPLE_RATE else Resampler(rate)
            self._rechunker = Rechunker()
            blocksize = int(round(FRAME_SAMPLES * rate / SAMPLE_RATE))
            stream = None
            try:
                stream = sd.InputStream(device=device.index, samplerate=rate, channels=1, dtype="float32",
                                        blocksize=blocksize, callback=self._callback, latency="low")
                stream.start()
                return stream
            except (sd.PortAudioError, ValueError) as exc:
                log.warning("Could not open %s at %d Hz: %s", device.name, rate, exc)
                if stream is not None:
                    _close_stream(stream)  # opened but failed to start: don't leak it
        raise AudioError(f"Could not open the microphone '{device.name}'.")

    def _callback(self, indata: np.ndarray, frames: int, time_info: object, status: sd.CallbackFlags) -> None:
        consumer = self._consumer
        if consumer is None:
            return
        try:
            mono = indata[:, 0].copy()
            if self._resampler is not None:
                mono = self._resampler.process(mono)
            for frame in self._rechunker.push(mono):
                consumer(frame)
        except Exception:  # noqa: BLE001 - an exception here would kill the stream
            self._callback_errors += 1
            if self._callback_errors <= 3:
                log.exception("Audio callback failed")
