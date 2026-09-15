"""End-to-end check with the real whisper-server and macOS text-to-speech.

Run with:  .venv/bin/python -m pytest -m integration -q
Needs: whisper-server (brew install whisper-cpp), the downloaded model, `say`.
"""

import shutil
import subprocess
import wave

import numpy as np
import pytest

from dikte import models, textproc
from dikte.config import Settings
from dikte.engines import create_engine
from dikte.engines.whisper_server import find_server_binary

pytestmark = pytest.mark.integration


def speech(tmp_path, voice: str, text: str) -> np.ndarray:
    path = tmp_path / f"{voice}.wav"
    subprocess.run(["say", "-v", voice, "-o", str(path), "--file-format=WAVE", "--data-format=LEI16@16000", text],
                   check=True)
    with wave.open(str(path)) as wav:
        return np.frombuffer(wav.readframes(wav.getnframes()), "<i2").astype(np.float32) / 32768


@pytest.fixture(scope="module")
def engine():
    if not (shutil.which("say") and find_server_binary() and models.is_downloaded(models.CATALOG["large-v3-turbo-q5_0"])):
        pytest.skip("whisper-server, model or `say` not available")
    eng = create_engine(Settings(), instance="test")  # never touch the running app's server
    eng.start()
    yield eng
    eng.stop()


def transcribe(engine, audio):
    settings = Settings()
    result = engine.transcribe(audio, "tr", textproc.build_prompt(settings, "tr"), repair=True)
    return textproc.process(result.text, settings).text, result


def test_turkish(engine, tmp_path):
    text, result = transcribe(engine, speech(tmp_path, "Yelda", "Merhaba, bu dosyadaki hatayı düzeltir misin?"))
    assert "hatayı düzeltir" in text
    assert result.elapsed_s < 5


def test_english_with_turkish_language_setting(engine, tmp_path):
    text, _ = transcribe(engine, speech(tmp_path, "Samantha", "Can you refactor the API endpoint?"))
    assert "refactor the API endpoint" in text


def test_mixed_trailing_english_is_recovered(engine, tmp_path):
    turkish = speech(tmp_path, "Yelda", "Pull request açmadan önce testleri çalıştır.")
    english = speech(tmp_path, "Samantha", "Also, please update the README.")
    text, result = transcribe(engine, np.concatenate([turkish, english]))
    assert "testleri çalıştır" in text
    assert "README" in text


def test_silence_produces_nothing(engine):
    noise = (np.random.default_rng(1).standard_normal(3 * 16000) * 0.003).astype(np.float32)
    text, _ = transcribe(engine, noise)
    assert text == ""
