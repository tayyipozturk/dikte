"""Find speech that Whisper skipped.

With mixed Turkish/English, Whisper sometimes stops after the first language
and silently drops a trailing sentence in the other one. Comparing its segment
timestamps with where the audio actually has speech reveals those gaps, which
are then transcribed on their own.
"""

from __future__ import annotations

from typing import Iterable

import numpy as np

from ..levels import SAMPLE_RATE, speech_regions
from .base import Segment

_TOLERANCE_S = 0.3  # timestamps are approximate
_MERGE_S = 0.5
_PAD_S = 0.15


def uncovered_speech(audio: np.ndarray, segments: Iterable[Segment],
                     min_gap_s: float = 0.6) -> list[tuple[float, float]]:
    covered = sorted((max(0.0, s.start - _TOLERANCE_S), s.end + _TOLERANCE_S) for s in segments)
    pieces: list[tuple[float, float]] = []
    for start, end in speech_regions(audio):
        cursor = start
        for c0, c1 in covered:
            if c1 <= cursor or c0 >= end:
                continue
            if c0 > cursor:
                pieces.append((cursor, c0))
            cursor = max(cursor, c1)
        if cursor < end:
            pieces.append((cursor, end))

    merged: list[tuple[float, float]] = []
    for piece in pieces:
        if merged and piece[0] - merged[-1][1] < _MERGE_S:
            merged[-1] = (merged[-1][0], piece[1])
        else:
            merged.append(piece)

    duration = len(audio) / SAMPLE_RATE
    return [
        (max(0.0, a - _PAD_S), min(duration, b + _PAD_S))
        for a, b in merged
        if b - a >= min_gap_s
    ]
