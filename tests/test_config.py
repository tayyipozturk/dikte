import json
from dataclasses import fields

import pytest

from dikte.config import SPECS, Settings, load_settings, save_settings, settings_from_dict, updated


def test_every_setting_has_a_validator_and_defaults_are_valid():
    defaults = Settings()
    names = {f.name for f in fields(Settings)}
    assert names == set(SPECS)
    for name in names:
        value = getattr(defaults, name)
        if name == "replacements":
            value = dict(value)
        elif isinstance(value, tuple):
            value = list(value)
        assert SPECS[name](value) is not None or value == ""


def test_missing_file_gives_defaults(tmp_path):
    settings, warnings = load_settings(tmp_path / "nope.json")
    assert settings == Settings()
    assert warnings == []


def test_invalid_values_fall_back_with_warnings():
    settings, warnings = settings_from_dict(
        {"mode": "shout", "silence_stop_s": -1, "sounds": "yes", "bogus": 1, "_comment": "ok"}
    )
    assert settings.mode == "hold"
    assert settings.silence_stop_s == Settings().silence_stop_s
    assert settings.sounds is True
    assert len(warnings) == 4
    assert any("bogus" in w for w in warnings)


def test_booleans_are_not_numbers():
    settings, warnings = settings_from_dict({"threads": True})
    assert settings.threads == Settings().threads
    assert warnings


def test_round_trip_keeps_turkish_text_and_replacements(tmp_path):
    path = tmp_path / "config.json"
    original = updated(
        Settings(),
        replacements={"jit hab": "GitHub", "yeni satır": "\n"},
        submit_phrases=["gönder"],
        prompt_tr="Şimdi İstanbul'da çalışalım.",
    )
    save_settings(original, path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["replacements"] == {"jit hab": "GitHub", "yeni satır": "\n"}
    assert "Şimdi" in path.read_text(encoding="utf-8")  # not \u-escaped
    loaded, warnings = load_settings(path)
    assert warnings == []
    assert loaded == original


def test_corrupt_file_gives_defaults_and_warning(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    settings, warnings = load_settings(path)
    assert settings == Settings()
    assert warnings and "config.json" in warnings[0]


def test_updated_rejects_bad_values():
    with pytest.raises(ValueError):
        updated(Settings(), mode="loud")
    with pytest.raises(ValueError):
        updated(Settings(), nonexistent=1)


def test_cloud_url_must_be_https_unless_local():
    assert updated(Settings(), cloud_base_url="http://localhost:8000/v1/").cloud_base_url == "http://localhost:8000/v1"
    with pytest.raises(ValueError):
        updated(Settings(), cloud_base_url="http://example.com/v1")


def test_unknown_model_is_rejected():
    settings, warnings = settings_from_dict({"local_model": "tiny-imaginary"})
    assert settings.local_model == Settings().local_model
    assert warnings
