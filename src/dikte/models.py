"""Catalog and downloader for whisper.cpp (ggml) models."""

from __future__ import annotations

import hashlib
import logging
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import paths

log = logging.getLogger(__name__)

_WHISPER = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
_VAD = "https://huggingface.co/ggml-org/whisper-vad/resolve/main"
_DISK_MARGIN_MB = 500


@dataclass(frozen=True)
class ModelInfo:
    key: str
    filename: str
    url: str
    size_mb: int
    note: str
    sha256: str  # Hugging Face LFS object id, verified after download


CATALOG: dict[str, ModelInfo] = {
    m.key: m
    for m in (
        ModelInfo(
            "large-v3-turbo-q5_0",
            "ggml-large-v3-turbo-q5_0.bin",
            f"{_WHISPER}/ggml-large-v3-turbo-q5_0.bin",
            547,
            "Recommended: fast, very good Turkish + English",
            "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2",
        ),
        ModelInfo(
            "large-v3-turbo-q8_0",
            "ggml-large-v3-turbo-q8_0.bin",
            f"{_WHISPER}/ggml-large-v3-turbo-q8_0.bin",
            833,
            "Slightly more precise, larger",
            "317eb69c11673c9de1e1f0d459b253999804ec71ac4c23c17ecf5fbe24e259a1",
        ),
        ModelInfo(
            "large-v3-turbo",
            "ggml-large-v3-turbo.bin",
            f"{_WHISPER}/ggml-large-v3-turbo.bin",
            1549,
            "Full precision turbo",
            "1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69",
        ),
        ModelInfo(
            "large-v3-q5_0",
            "ggml-large-v3-q5_0.bin",
            f"{_WHISPER}/ggml-large-v3-q5_0.bin",
            1031,
            "Most accurate, about 2x slower",
            "d75795ecff3f83b5faa89d1900604ad8c780abd5739fae406de19f23ecd98ad1",
        ),
        ModelInfo(
            "base-q5_1",
            "ggml-base-q5_1.bin",
            f"{_WHISPER}/ggml-base-q5_1.bin",
            57,
            "Tiny and fast, much less accurate (slow machines, testing)",
            "422f1ae452ade6f30a004d7e5c6a43195e4433bc370bf23fac9cc591f01a8898",
        ),
    )
}

VAD_MODEL = ModelInfo(
    "silero-v6.2.0",
    "ggml-silero-v6.2.0.bin",
    f"{_VAD}/ggml-silero-v6.2.0.bin",
    1,
    "Silero voice activity detection (filters silence and noise)",
    "2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987",
)


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, PLR0913
        if not newurl.lower().startswith("https://"):
            raise urllib.error.HTTPError(newurl, code, "Refusing a non-HTTPS redirect", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_HttpsOnlyRedirect())

ProgressFn = Callable[[int, int], None]  # (bytes_done, bytes_total)


def model_path(info: ModelInfo) -> Path:
    return paths.models_dir() / info.filename


def is_downloaded(info: ModelInfo) -> bool:
    path = model_path(info)
    return path.exists() and path.stat().st_size > 0


def download(info: ModelInfo, progress: ProgressFn | None = None, dest_dir: Path | None = None) -> Path:
    """Download (resuming a partial file) and return the final path."""
    dest_dir = dest_dir or paths.models_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / info.filename
    if final.exists() and final.stat().st_size > 0:
        return final

    part = final.with_name(final.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    needed = (info.size_mb + _DISK_MARGIN_MB) * 1024 * 1024 - have
    free = shutil.disk_usage(dest_dir).free
    if free < needed:
        raise OSError(
            f"Not enough disk space for {info.filename}: need ~{needed // 2**20} MB, "
            f"have {free // 2**20} MB free."
        )

    headers = {"Range": f"bytes={have}-"} if have else {}
    request = urllib.request.Request(info.url, headers=headers)
    log.info("Downloading %s (%d MB)", info.filename, info.size_mb)
    with _OPENER.open(request, timeout=30) as response:
        if have and response.status != 206:
            have = 0  # server ignored the Range header: start over
        total = have + int(response.headers.get("Content-Length") or 0)
        with open(part, "ab" if have else "wb") as out:
            done = have
            while chunk := response.read(1 << 20):
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)

    if total and part.stat().st_size != total:
        raise OSError(f"Download of {info.filename} is incomplete; run it again to resume.")
    if _sha256(part) != info.sha256:
        part.unlink()  # corrupt or tampered: never keep it, even for resuming
        raise OSError(f"Checksum mismatch for {info.filename}; the download was discarded. Try again.")
    part.replace(final)
    return final


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()
