"""Regression tests for the security review findings."""

import hashlib
import os
import urllib.request
from pathlib import Path

import numpy as np
import pytest

from dikte import models
from dikte.audio import Resampler
from dikte.engines import http
from dikte.engines.whisper_server import WhisperServerEngine
from dikte.feedback import _applescript_string
from dikte.levels import SAMPLE_RATE, level_db


def test_loopback_requests_bypass_proxies(monkeypatch):
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:3128")
    with_proxy = urllib.request.build_opener()
    assert any(isinstance(h, urllib.request.ProxyHandler) for h in with_proxy.handlers)
    # An empty ProxyHandler replaces the default one and registers no proxy routes.
    assert not any(isinstance(h, urllib.request.ProxyHandler) for h in http._LOCAL_OPENER.handlers)


def test_redirects_are_refused():
    assert any(isinstance(h, http._NoRedirect) for h in http._REMOTE_OPENER.handlers)
    assert http._NoRedirect().redirect_request(None, None, 302, "Found", {}, "http://evil.example/") is None


def test_error_text_is_scrubbed():
    assert "abc123def456" not in http.scrub("Incorrect API key provided: sk-proj-abc123def456")
    assert http.scrub("token mysecret123 here", "mysecret123") == "token *** here"
    assert http._error_detail(b'{"error": {"message": "Invalid file format"}}') == "Invalid file format"


def test_notification_text_has_no_control_characters():
    assert "\x00" not in _applescript_string("bad\x00text\nline")


def test_download_with_wrong_checksum_is_discarded(tmp_path, monkeypatch):
    payload = b"not really a model" * 100
    source = tmp_path / "model.bin"
    source.write_bytes(payload)
    monkeypatch.setattr(models, "_OPENER", urllib.request.build_opener())
    good = models.ModelInfo("t", "t.bin", source.as_uri(), 1, "", hashlib.sha256(payload).hexdigest())
    assert models.download(good, dest_dir=tmp_path / "ok").read_bytes() == payload
    bad = models.ModelInfo("t", "t.bin", source.as_uri(), 1, "", "0" * 64)
    with pytest.raises(OSError, match="Checksum"):
        models.download(bad, dest_dir=tmp_path / "bad")
    assert not any((tmp_path / "bad").iterdir())


def test_stale_pid_without_matching_token_is_not_killed(tmp_path):
    pid_file = tmp_path / "server.pid"
    pid_file.write_text(f"{os.getpid()} some-other-token")
    engine = WhisperServerEngine("/bin/false", Path("/nonexistent"), None, 1, tmp_path / "log", pid_file)
    engine._kill_stale()  # would SIGTERM this test process if the check were wrong
    pid_file.write_text(str(os.getpid()))  # old format without a token: never guessed
    engine._kill_stale()


def test_upsampling_does_not_boost_treble():
    rate = 8000
    t = np.arange(rate) / rate
    source = (0.1 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
    out = Resampler(rate).process(source)
    change = level_db(out[2000:14000]) - level_db(source)
    assert -1.5 < change < 0.5  # the old filter boosted this by ~+6 dB
    assert abs(len(out) - SAMPLE_RATE) < 10
