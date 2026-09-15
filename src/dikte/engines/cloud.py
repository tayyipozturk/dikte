"""Cloud transcription through an OpenAI-compatible /audio/transcriptions API.

Default: OpenAI gpt-transcribe, which officially supports code-switching and
takes language hints (languages[]) plus keywords[]. Other compatible services
(e.g. Groq's whisper-large-v3) work by changing cloud_base_url / cloud_model.

The API key comes from the macOS Keychain (the account name is the value of
cloud_api_key_env):
    security add-generic-password -s Dikte -a OPENAI_API_KEY -w
An environment variable of that name also works, but only when Dikte runs
from a terminal: Dikte.app gives Python a minimal environment on purpose.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time

import numpy as np

from ..levels import SAMPLE_RATE
from .base import EngineError, Transcript
from .http import post_multipart, scrub, wav_bytes

log = logging.getLogger(__name__)

KEYCHAIN_SERVICE = "Dikte"


def resolve_api_key(env_name: str) -> str | None:
    key = os.environ.get(env_name, "").strip()
    if key:
        return key
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", env_name, "-w"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    key = result.stdout.strip()
    return key if result.returncode == 0 and key else None


def _language_fields(model: str, language: str) -> list[tuple[str, str]]:
    if model.startswith("gpt-transcribe"):
        # gpt-transcribe takes languages[] *instead of* language.
        hints = {"tr": ("tr", "en"), "en": ("en",)}.get(language, ())
        return [("languages[]", code) for code in hints]
    if language in ("tr", "en") and "whisper" in model:
        return [("language", language)]  # Whisper keeps English words when told "tr"
    if language == "en":
        return [("language", "en")]
    return []


class OpenAICompatibleEngine:
    name = "cloud"

    def __init__(self, base_url: str, model: str, api_key_env: str, keywords: tuple[str, ...] = ()) -> None:
        self._url = f"{base_url.rstrip('/')}/audio/transcriptions"
        self._model = model
        self._api_key_env = api_key_env
        self._keywords = tuple(k for k in keywords if not re.search(r"[<>\r\n]", k))[:50]
        self._key: str | None = None

    def start(self) -> None:
        self._key = resolve_api_key(self._api_key_env)
        if not self._key:
            raise EngineError(
                f"No API key. Store it in the Keychain: "
                f"security add-generic-password -s {KEYCHAIN_SERVICE} -a {self._api_key_env} -w"
            )

    def stop(self) -> None:
        self._key = None

    def transcribe(self, audio: np.ndarray, language: str, prompt: str, repair: bool = True) -> Transcript:
        if not self._key:
            self.start()
        started = time.monotonic()
        fields = [("model", self._model)] + _language_fields(self._model, language)
        if self._model.startswith("gpt-transcribe"):
            fields += [("keywords[]", k) for k in self._keywords]
        else:
            fields += [("response_format", "json"), ("temperature", "0")]
        if prompt:
            fields.append(("prompt", prompt))
        files = [("file", "audio.wav", wav_bytes(audio), "audio/wav")]
        headers = {"Authorization": f"Bearer {self._key}"}

        for attempt in range(2):
            try:
                timeout = max(60.0, 30.0 + len(audio) / SAMPLE_RATE)  # upload + processing time
                data = post_multipart(self._url, fields, files, headers=headers, timeout=timeout)
                break
            except EngineError as exc:
                message = scrub(str(exc), self._key)  # some gateways echo the key back
                if attempt == 1 or not exc.retryable:
                    raise EngineError(message, exc.retryable, exc.timed_out) from None
                log.warning("Cloud transcription failed (%s); retrying once", message)
                time.sleep(1.0)

        return Transcript(
            text=str(data.get("text") or ""),
            language=None if language == "auto" else language,
            duration_s=len(audio) / SAMPLE_RATE,
            elapsed_s=time.monotonic() - started,
        )
