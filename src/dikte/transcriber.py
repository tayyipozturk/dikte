"""Serial transcription worker. Owns the engine; turns audio into inserted text.

Jobs run one at a time, so text from consecutive dictations is always inserted
in the order it was spoken.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, Protocol

import numpy as np

from . import paths, textproc
from .config import Settings
from .engines import Engine, EngineError, create_engine, engine_signature
from .engines.http import wav_bytes

log = logging.getLogger(__name__)


class Inserter(Protocol):
    def can_insert(self) -> bool: ...
    def frontmost_pid(self) -> int | None: ...
    def insert(self, text: str, settings: Settings, submit: bool) -> None: ...
    def copy(self, text: str) -> None: ...


@dataclass(frozen=True)
class Job:
    audio: np.ndarray
    settings: Settings
    target_pid: int | None = None
    peak_db: float = 0.0


@dataclass(frozen=True)
class JobResult:
    text: str = ""
    submitted: bool = False
    copied_only: bool = False  # copied to the clipboard instead of inserted
    copy_reason: str = ""  # "app_changed" | "no_permission"
    error: str = ""
    elapsed_s: float = 0.0
    repaired: int = 0
    peak_db: float = 0.0


EngineFactory = Callable[[Settings], Engine]


class Transcriber:
    def __init__(self, inserter: Inserter, on_result: Callable[[JobResult], None],
                 on_engine_state: Callable[[bool, str], None],
                 engine_factory: EngineFactory = create_engine) -> None:
        self._queue: queue.Queue[tuple] = queue.Queue()
        self._inserter = inserter
        self._on_result = on_result
        self._on_state = on_engine_state
        self._factory = engine_factory
        self._engine: Engine | None = None
        self._signature: tuple | None = None
        self._thread: threading.Thread | None = None
        self._stopping = False

    def start(self, settings: Settings) -> None:
        self._thread = threading.Thread(target=self._run, name="dikte-transcriber", daemon=True)
        self._thread.start()
        self.configure(settings)

    def configure(self, settings: Settings) -> None:
        self._queue.put(("configure", settings))

    def submit(self, job: Job) -> None:
        self._queue.put(("job", job))

    def insert_text(self, text: str, settings: Settings) -> None:
        self._queue.put(("insert", text, settings))

    def stop(self, timeout: float = 5.0) -> None:
        """Drop queued work (nothing is pasted while quitting) and stop the engine,
        directly if the worker is stuck in a long request."""
        self._stopping = True
        with contextlib.suppress(queue.Empty):
            while True:
                self._queue.get_nowait()
        self._queue.put(("stop",))
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                log.warning("Transcriber still busy; stopping the speech engine directly")
                self._stop_engine()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            kind = item[0]
            if kind == "stop":
                break
            if self._stopping:
                continue
            try:
                if kind == "configure":
                    self._configure(item[1])
                elif kind == "job":
                    self._on_result(self._process(item[1]))
                elif kind == "insert":
                    time.sleep(0.25)  # let the menu close so focus is back in the target app
                    self._inserter.insert(item[1], item[2], submit=False)
            except Exception:  # noqa: BLE001 - the worker must survive anything
                log.exception("Transcriber failed while handling %s", kind)
                if kind == "job":
                    self._on_result(JobResult(error="Unexpected error (see log)"))
        self._stop_engine()

    def _configure(self, settings: Settings) -> None:
        signature = engine_signature(settings)
        if signature == self._signature and self._engine is not None:
            return
        self._stop_engine()
        self._signature = signature
        self._start_engine(settings)

    def _start_engine(self, settings: Settings) -> bool:
        self._on_state(False, "Loading speech model…")
        engine = self._factory(settings)
        self._engine = engine  # reachable by stop() while it is still starting
        try:
            engine.start()
        except Exception as exc:  # noqa: BLE001 - EngineError, or OSError from a broken setup
            log.error("Speech engine failed to start: %s", exc)
            self._engine = None
            with contextlib.suppress(Exception):
                engine.stop()  # never leave a half-started server behind
            self._on_state(False, str(exc) or exc.__class__.__name__)
            return False
        if self._stopping:  # quit arrived while the model was loading
            self._stop_engine()
            return False
        self._on_state(True, "")
        return True

    def _stop_engine(self) -> None:
        engine, self._engine = self._engine, None
        if engine is None:
            return
        try:
            engine.stop()
        except Exception:  # noqa: BLE001
            log.exception("Stopping the speech engine failed")

    def _process(self, job: Job) -> JobResult:
        settings = job.settings
        if settings.keep_last_recording:
            _save_recording(job.audio)
        if self._engine is None and not self._start_engine(settings):
            return JobResult(error="Speech engine is not available", peak_db=job.peak_db)
        assert self._engine is not None
        prompt = textproc.build_prompt(settings, settings.language)
        try:
            transcript = self._engine.transcribe(job.audio, settings.language, prompt, repair=settings.repair_gaps)
        except EngineError as exc:
            return JobResult(error=f"Transcription failed: {exc}", peak_db=job.peak_db)

        processed = textproc.process(transcript.text, settings)
        log.info("Transcribed %.1f s of audio in %.2f s (%d chars, %d gap(s) repaired)",
                 transcript.duration_s, transcript.elapsed_s, len(processed.text), transcript.repaired)
        log.debug("Transcript: %r", processed.text)
        base = JobResult(text=processed.text, elapsed_s=transcript.elapsed_s,
                         repaired=transcript.repaired, peak_db=job.peak_db)
        if (not processed.text and not processed.submit) or self._stopping:
            return base

        reason = ""
        if not self._inserter.can_insert():
            reason = "no_permission"
        elif settings.paste_guard and job.target_pid is not None:
            current = self._inserter.frontmost_pid()
            if current is not None and current != job.target_pid:
                reason = "app_changed"
        if reason:
            if processed.text:
                self._inserter.copy(processed.text)
            return replace(base, copied_only=True, copy_reason=reason)
        self._inserter.insert(processed.text, settings, processed.submit)
        return replace(base, submitted=processed.submit)


def _save_recording(audio: np.ndarray) -> None:
    try:
        paths.last_recording_path().write_bytes(wav_bytes(audio))
    except OSError:
        log.warning("Could not save the last recording", exc_info=True)
