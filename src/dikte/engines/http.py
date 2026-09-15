"""Minimal multipart HTTP client (stdlib only) and WAV encoding.

Loopback requests never go through a proxy (so "local" audio stays on this
Mac), redirects are refused (so an API key is never forwarded elsewhere), and
error text from servers is trimmed and scrubbed of anything key-like.
"""

from __future__ import annotations

import io
import json
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
import wave
from typing import Any

import numpy as np

from .base import EngineError

FilePart = tuple[str, str, bytes, str]  # (field, filename, data, content type)

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_SECRET_RE = re.compile(r"\b(sk|gsk|key|xai)[-_][A-Za-z0-9_\-*]{6,}", re.IGNORECASE)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, PLR0913
        return None  # urllib then raises HTTPError for the 3xx response


_REMOTE_OPENER = urllib.request.build_opener(_NoRedirect())
_LOCAL_OPENER = urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({}))


def scrub(text: str, secret: str | None = None) -> str:
    """Remove API keys (the given one and anything that looks like one)."""
    if secret:
        text = text.replace(secret, "***")
    return _SECRET_RE.sub(lambda m: m.group(1) + "-***", text)


def wav_bytes(audio: np.ndarray, sample_rate: int = 16000) -> bytes:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(pcm.tobytes())
    return buffer.getvalue()


def encode_multipart(fields: list[tuple[str, str]], files: list[FilePart]) -> tuple[bytes, str]:
    boundary = "dikte-" + secrets.token_hex(16)
    parts = []
    for name, value in fields:
        header = f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
        parts.append(header.encode() + value.encode("utf-8") + b"\r\n")
    for name, filename, data, content_type in files:
        header = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        )
        parts.append(header.encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _error_detail(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text[:200]
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        error = error.get("message")
    return str(error or text)[:200]


def post_multipart(
    url: str,
    fields: list[tuple[str, str]],
    files: list[FilePart],
    headers: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    body, content_type = encode_multipart(fields, files)
    request = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": content_type, **(headers or {})}
    )
    host = urllib.parse.urlsplit(url).hostname or ""
    opener = _LOCAL_OPENER if host in _LOOPBACK else _REMOTE_OPENER
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        detail = scrub(_error_detail(exc.read(2000)))
        raise EngineError(f"HTTP {exc.code}: {detail}", retryable=exc.code >= 500) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        timed_out = isinstance(reason, TimeoutError) or "timed out" in str(reason)
        raise EngineError(f"Request failed: {reason}", retryable=True, timed_out=timed_out) from None
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise EngineError("Invalid JSON in response") from None
    if not isinstance(data, dict):
        raise EngineError("Unexpected response format")
    if data.get("error") and not data.get("text"):
        raise EngineError(f"Server error: {scrub(str(data['error']))[:200]}")
    return data
