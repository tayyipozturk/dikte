"""Transcription engines: local whisper.cpp server or an OpenAI-compatible cloud API."""

from __future__ import annotations

from .. import paths
from ..config import Settings
from ..models import CATALOG, VAD_MODEL, model_path
from .base import Engine, EngineError, Segment, Transcript
from .cloud import OpenAICompatibleEngine
from .whisper_server import WhisperServerEngine, find_server_binary

__all__ = ["Engine", "EngineError", "Segment", "Transcript", "create_engine", "engine_signature"]


def engine_signature(settings: Settings) -> tuple:
    """Settings that require re-creating the engine when they change."""
    if settings.engine == "cloud":
        return ("cloud", settings.cloud_base_url, settings.cloud_model,
                settings.cloud_api_key_env, settings.vocabulary)
    return ("local", settings.local_model, settings.whisper_server_path, settings.threads)


def create_engine(settings: Settings, instance: str = "app") -> Engine:
    if settings.engine == "cloud":
        return OpenAICompatibleEngine(
            settings.cloud_base_url, settings.cloud_model, settings.cloud_api_key_env, settings.vocabulary
        )
    return WhisperServerEngine(
        binary=find_server_binary(settings.whisper_server_path),
        model_path=model_path(CATALOG[settings.local_model]),
        vad_model_path=model_path(VAD_MODEL),
        threads=settings.threads,
        log_path=paths.log_dir() / f"whisper-server-{instance}.log",
        pid_path=paths.server_pid_path(instance),
    )
