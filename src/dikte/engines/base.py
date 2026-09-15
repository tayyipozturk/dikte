"""Shared types for transcription engines."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

import numpy as np


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str
    no_speech_prob: float = 0.0
    avg_logprob: float = 0.0

    def shifted(self, offset: float) -> Segment:
        return replace(self, start=self.start + offset, end=self.end + offset)


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str | None = None
    segments: tuple[Segment, ...] = ()
    duration_s: float = 0.0
    elapsed_s: float = 0.0
    repaired: int = 0  # number of skipped speech gaps that were re-transcribed


class EngineError(Exception):
    def __init__(self, message: str, retryable: bool = False, timed_out: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.timed_out = timed_out


class Engine(Protocol):
    name: str

    def start(self) -> None:
        """Prepare the engine (may block while a model loads)."""

    def stop(self) -> None: ...

    def transcribe(self, audio: np.ndarray, language: str, prompt: str, repair: bool = True) -> Transcript:
        """audio: mono float32 at 16 kHz. language: "tr", "en" or "auto"."""
