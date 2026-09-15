"""Local transcription with whisper.cpp's whisper-server (Metal GPU).

The server keeps the model in memory, so a typical dictation takes about one
second on Apple Silicon. It listens on 127.0.0.1 only, under a random secret URL
prefix, which keeps web pages and sandboxed apps from using or reconfiguring
it (the prefix is visible in `ps`, so it is not a secret from your own
processes or other accounts on this Mac). Silero VAD
inside the server drops silence and noise before Whisper sees it, which
prevents hallucinations such as "Altyazı M.K.".
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

import numpy as np

from .. import paths
from ..levels import SAMPLE_RATE
from .base import EngineError, Segment, Transcript
from .http import post_multipart, wav_bytes
from .repair import uncovered_speech

log = logging.getLogger(__name__)

_CANDIDATES = (
    "/opt/homebrew/bin/whisper-server",  # macOS (Homebrew)
    "/usr/local/bin/whisper-server",
    "/usr/bin/whisper-server",  # Ubuntu 26.04 (apt)
)
_LANGUAGE_CODES = {"turkish": "tr", "english": "en"}
_START_TIMEOUT_S = 60.0
_MAX_REPAIRS = 2
_MAX_LOG_BYTES = 5 * 1024 * 1024


def find_server_binary(configured: str = "") -> str | None:
    if configured:
        return configured if os.access(configured, os.X_OK) else None
    bundled = paths.whisper_dir() / "whisper-server"  # unpacked by the Linux installer
    if os.access(bundled, os.X_OK):
        return str(bundled)
    found = shutil.which("whisper-server")  # PATH is minimal when started by launchd
    if found:
        return found
    return next((c for c in _CANDIDATES if os.access(c, os.X_OK)), None)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _is_silence(segment: Segment) -> bool:
    # Whisper's own rule for segments that are probably not speech.
    return segment.no_speech_prob > 0.6 and segment.avg_logprob < -1.0


class WhisperServerEngine:
    name = "local"

    def __init__(self, binary: str | None, model_path: Path, vad_model_path: Path | None,
                 threads: int, log_path: Path, pid_path: Path) -> None:
        self._binary = binary
        self._model_path = model_path
        self._vad_model_path = vad_model_path
        self._threads = threads
        self._log_path = log_path
        self._pid_path = pid_path
        self._proc: subprocess.Popen[bytes] | None = None
        self._port = 0
        self._token = ""
        self._stopped = False

    # lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        if not self._binary:
            raise EngineError("whisper-server not found. Install it with: brew install whisper-cpp")
        if not self._model_path.exists():
            raise EngineError(f"Model file missing: {self._model_path.name}. Run: uv run dikte download")
        if self._stopped:  # stopped engines are never reused; a new one is created instead
            raise EngineError("Speech engine was stopped")
        self._kill_stale()
        port = _free_port()
        token = secrets.token_urlsafe(24)
        args = [
            self._binary, "-m", str(self._model_path), "--host", "127.0.0.1", "--port", str(port),
            "--request-path", f"/{token}",  # other local processes cannot reach the API
            "-t", str(self._threads),
            "-sns",  # suppress non-speech tokens like [Music]
            "-nlp",  # skip language probabilities (saves a full encoder pass)
        ]
        if self._vad_model_path and self._vad_model_path.exists():
            args += ["--vad", "--vad-model", str(self._vad_model_path)]
        else:
            log.warning("VAD model missing: silence may produce hallucinated text")

        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            too_big = self._log_path.exists() and self._log_path.stat().st_size > _MAX_LOG_BYTES
            with open(self._log_path, "wb" if too_big else "ab") as server_log:
                self._proc = subprocess.Popen(
                    args, stdin=subprocess.DEVNULL, stdout=server_log, stderr=subprocess.STDOUT, close_fds=True
                )
        except OSError as exc:
            raise EngineError(f"Could not start whisper-server: {exc}") from None
        if self._stopped:  # stop() raced with this start (app quitting)
            self._shutdown_process()
            raise EngineError("Speech engine was stopped")
        try:
            self._pid_path.write_text(f"{self._proc.pid} {token}")
        except OSError:
            log.warning("Could not write %s", self._pid_path)
        self._wait_ready(port)
        self._port, self._token = port, token
        log.info("whisper-server ready on port %d (pid %d)", port, self._proc.pid)

    def stop(self) -> None:
        """Stop for good: in-flight requests fail instead of restarting the server."""
        self._stopped = True
        self._shutdown_process()

    def _shutdown_process(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        with contextlib.suppress(OSError):
            self._pid_path.unlink()

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _wait_ready(self, port: int) -> None:
        deadline = time.monotonic() + _START_TIMEOUT_S
        while time.monotonic() < deadline:
            proc = self._proc
            if self._stopped or proc is None:
                self._shutdown_process()
                raise EngineError("Speech engine was stopped")
            if proc.poll() is not None:
                raise EngineError(f"whisper-server exited with code {proc.returncode}; see {self._log_path}")
            with contextlib.suppress(OSError):
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                return
            time.sleep(0.1)
        self._shutdown_process()
        raise EngineError("whisper-server did not start within 60 s")

    def _kill_stale(self) -> None:
        """Stop a server left behind by a crashed previous run. The pid file holds
        the server's secret token, so an unrelated process is never killed."""
        try:
            pid_text, _, token = self._pid_path.read_text().strip().partition(" ")
            pid = int(pid_text)
        except (OSError, ValueError):
            return
        if not token:
            return
        try:
            command = subprocess.run(["ps", "-p", str(pid), "-o", "args="], capture_output=True,
                                     text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            return
        if f"/{token}" in command:
            log.info("Stopping stale whisper-server (pid %d)", pid)
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)

    # transcription -----------------------------------------------------------

    def transcribe(self, audio: np.ndarray, language: str, prompt: str, repair: bool = True) -> Transcript:
        started = time.monotonic()
        request_language = language if language in ("tr", "en") else "auto"
        segments, detected = self._request(audio, request_language, prompt)
        if language == "auto" and segments and detected not in ("tr", "en"):
            log.info("Detected %r; retrying as Turkish", detected)
            segments, detected = self._request(audio, "tr", prompt)

        repaired = 0
        if repair and segments:
            gap_language = request_language if request_language != "auto" else (detected or "tr")
            for start, end in uncovered_speech(audio, segments)[:_MAX_REPAIRS]:
                clip = audio[int(start * SAMPLE_RATE): int(end * SAMPLE_RATE)]
                extra, _ = self._request(clip, gap_language, prompt)
                if extra:
                    segments += [s.shifted(start) for s in extra]
                    repaired += 1
                    log.info("Recovered %.1f s of speech Whisper skipped", end - start)
            segments.sort(key=lambda s: s.start)

        kept = tuple(s for s in segments if s.text.strip() and not _is_silence(s))
        return Transcript(
            text=" ".join(s.text.strip() for s in kept),
            language=detected,
            segments=kept,
            duration_s=len(audio) / SAMPLE_RATE,
            elapsed_s=time.monotonic() - started,
            repaired=repaired,
        )

    def _request(self, audio: np.ndarray, language: str, prompt: str) -> tuple[list[Segment], str | None]:
        fields = [
            ("response_format", "verbose_json"),
            ("temperature", "0.0"),
            ("temperature_inc", "0.2"),
            ("language", language),
        ]
        if prompt:
            fields.append(("prompt", prompt))
        files = [("file", "audio.wav", wav_bytes(audio), "audio/wav")]
        for attempt in range(2):
            if not self.is_running():
                if self._stopped:
                    raise EngineError("Speech engine was stopped")
                log.warning("whisper-server is not running; restarting")
                self._shutdown_process()
                self.start()
            try:
                url = f"http://127.0.0.1:{self._port}/{self._token}/inference"
                timeout = max(120.0, 60.0 + len(audio) / SAMPLE_RATE)  # long recordings take longer
                data = post_multipart(url, fields, files, timeout=timeout)
                break
            except EngineError as exc:
                if exc.timed_out and self.is_running():
                    log.warning("whisper-server stopped responding; it will be restarted")
                    self._shutdown_process()  # the next request starts a fresh one
                    raise
                if attempt == 1 or not exc.retryable or self.is_running():
                    raise
        segments = [
            Segment(
                start=float(s.get("start") or 0.0),
                end=float(s.get("end") or 0.0),
                text=str(s.get("text") or ""),
                no_speech_prob=float(s.get("no_speech_prob") or 0.0),
                avg_logprob=float(s.get("avg_logprob") or 0.0),
            )
            for s in data.get("segments") or []
            if isinstance(s, dict)
        ]
        raw_language = str(data.get("language") or "").lower()
        return segments, _LANGUAGE_CODES.get(raw_language, raw_language or None)
