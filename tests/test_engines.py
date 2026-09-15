import io
import wave
from email.parser import BytesParser
from email.policy import default

import numpy as np

from dikte.audio import Rechunker, Resampler
from dikte.engines.base import Segment
from dikte.engines.cloud import _language_fields
from dikte.engines.http import encode_multipart, wav_bytes
from dikte.engines.repair import uncovered_speech
from dikte.inserter import _utf16_chunks
from dikte.levels import SAMPLE_RATE

RNG = np.random.default_rng(11)


def tone(seconds, db=-20.0, hz=220.0, rate=SAMPLE_RATE):
    t = np.arange(int(seconds * rate)) / rate
    return (10 ** (db / 20) * np.sqrt(2) * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def quiet(seconds):
    return (RNG.standard_normal(int(seconds * SAMPLE_RATE)) * 10 ** (-70 / 20)).astype(np.float32)


def test_wav_bytes_round_trip_and_clipping():
    audio = np.array([0.0, 0.5, -0.5, 2.0, -2.0], np.float32)
    with wave.open(io.BytesIO(wav_bytes(audio))) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        pcm = np.frombuffer(wav.readframes(5), "<i2")
    assert list(pcm) == [0, 16383, -16383, 32767, -32767]


def test_multipart_encodes_repeated_fields_and_unicode():
    body, content_type = encode_multipart(
        [("languages[]", "tr"), ("languages[]", "en"), ("prompt", "Şimdi test")],
        [("file", "audio.wav", b"RIFFdata", "audio/wav")],
    )
    message = BytesParser(policy=default).parsebytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    parts = list(message.iter_parts())
    assert [p.get_param("name", header="content-disposition") for p in parts] == ["languages[]", "languages[]", "prompt", "file"]
    assert parts[2].get_payload(decode=True) == "Şimdi test".encode("utf-8")
    assert parts[3].get_content() == b"RIFFdata"


def test_uncovered_trailing_speech_is_found():
    audio = np.concatenate([tone(3.0), quiet(0.4), tone(2.0), quiet(0.3)])
    gaps = uncovered_speech(audio, [Segment(0.0, 3.0, "Türkçe kısım")])
    assert len(gaps) == 1
    start, end = gaps[0]
    assert 3.1 < start < 3.6 and end > 5.2


def test_fully_covered_speech_needs_no_repair():
    audio = np.concatenate([tone(3.0), quiet(0.4), tone(2.0)])
    assert uncovered_speech(audio, [Segment(0.0, 3.0, "a"), Segment(3.0, 5.4, "b")]) == []


def test_cloud_language_fields():
    assert _language_fields("gpt-transcribe", "tr") == [("languages[]", "tr"), ("languages[]", "en")]
    assert _language_fields("gpt-transcribe", "auto") == []
    assert _language_fields("whisper-large-v3", "tr") == [("language", "tr")]
    assert _language_fields("gpt-4o-transcribe", "tr") == []
    assert _language_fields("gpt-4o-transcribe", "en") == [("language", "en")]


def test_rechunker_emits_fixed_frames():
    chunker = Rechunker(512)
    frames = chunker.push(np.ones(700, np.float32)) + chunker.push(np.ones(400, np.float32))
    assert [len(f) for f in frames] == [512, 512]


def test_resampler_keeps_frequency_and_length():
    resampler = Resampler(48000)
    source = tone(1.0, hz=440.0, rate=48000)
    out = np.concatenate([resampler.process(block) for block in np.array_split(source, 37)])
    assert abs(len(out) - SAMPLE_RATE) < 40
    spectrum = np.abs(np.fft.rfft(out[1000:9000]))
    peak_hz = np.argmax(spectrum) * SAMPLE_RATE / 8000
    assert abs(peak_hz - 440) < 5


def test_utf16_chunks_never_split_surrogates():
    text = "Şimdi 😀" * 6
    chunks = _utf16_chunks(text, 20)
    assert "".join(chunks) == text
    assert all(len(c.encode("utf-16-le")) // 2 <= 20 for c in chunks)
