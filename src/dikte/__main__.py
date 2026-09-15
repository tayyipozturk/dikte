"""Command line. `dikte` (or `dikte run`) starts the menu-bar app.

  dikte download [MODEL]   download a speech model (+ the VAD model)
  dikte transcribe FILE    transcribe an audio file with the current settings
  dikte devices            list microphones
  dikte doctor             check permissions, models and setup
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import wave

import numpy as np

from . import models, paths
from .config import LANGUAGES, load_settings, updated


def _progress(done: int, total: int) -> None:
    if total:
        sys.stdout.write(f"\r  {done * 100 // total:3d}%  {done // 2**20} / {total // 2**20} MB")
        sys.stdout.flush()


def cmd_download(model: str | None) -> int:
    settings, _ = load_settings()
    for info in (models.CATALOG[model or settings.local_model], models.VAD_MODEL):
        if models.is_downloaded(info):
            print(f"✓ {info.filename} already downloaded")
            continue
        print(f"Downloading {info.filename} (~{info.size_mb} MB)")
        try:
            models.download(info, _progress)
        except OSError as exc:
            print(f"\n✗ {exc}")
            return 1
        print(f"\n✓ {info.filename}")
    return 0


def _load_audio(path: str) -> np.ndarray:
    if shutil.which("ffmpeg"):
        raw = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-i", path, "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
            capture_output=True, check=True,
        ).stdout
        return np.frombuffer(raw, dtype=np.float32).copy()
    with wave.open(path) as wav:
        if wav.getsampwidth() != 2 or wav.getframerate() != 16000:
            raise SystemExit("Without ffmpeg only 16 kHz 16-bit WAV files are supported")
        pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        return (pcm.reshape(-1, wav.getnchannels()).mean(axis=1) / 32768.0).astype(np.float32)


def cmd_transcribe(path: str, language: str | None) -> int:
    from . import textproc
    from .engines import EngineError, create_engine

    settings, _ = load_settings()
    if language:
        settings = updated(settings, language=language)
    audio = _load_audio(path)
    engine = create_engine(settings, instance="cli")
    try:
        engine.start()
        result = engine.transcribe(audio, settings.language, textproc.build_prompt(settings, settings.language),
                                   repair=settings.repair_gaps)
    except EngineError as exc:
        print(f"✗ {exc}")
        return 1
    finally:
        engine.stop()
    processed = textproc.process(result.text, settings)
    print(processed.text)
    print(f"[{result.duration_s:.1f} s audio, {result.elapsed_s:.2f} s, language={result.language}, "
          f"repaired={result.repaired}, submit={processed.submit}]", file=sys.stderr)
    return 0


def cmd_devices() -> int:
    from .audio import list_input_devices, pick_device

    settings, _ = load_settings()
    devices = list_input_devices()
    chosen = pick_device(devices, settings.input_device)
    for device in devices:
        mark = "→" if chosen and device.index == chosen.index else " "
        print(f"{mark} [{device.index}] {device.name}  ({device.sample_rate:.0f} Hz, {device.channels} ch)")
    if not devices:
        print("No microphones found.")
    return 0


def cmd_doctor() -> int:
    from . import login_item, permissions
    from .engines.whisper_server import find_server_binary

    settings, warnings = load_settings()
    perms = permissions.check()

    def line(ok: bool, text: str) -> None:
        print(f"{'✓' if ok else '✗'} {text}")

    print("Permissions (they belong to the app that launched this command):")
    line(perms.microphone == "granted", f"Microphone: {perms.microphone}")
    line(perms.accessibility, "Accessibility (insert text)")
    line(perms.input_monitoring, "Input Monitoring (hotkey; may be covered by Accessibility)")
    line(not permissions.secure_input_enabled(), "Secure Keyboard Entry is off")
    print("\nSpeech:")
    server = find_server_binary(settings.whisper_server_path)
    line(server is not None, f"whisper-server: {server or 'missing (brew install whisper-cpp)'}")
    model = models.CATALOG[settings.local_model]
    line(models.is_downloaded(model), f"Model {model.filename}")
    line(models.is_downloaded(models.VAD_MODEL), f"VAD model {models.VAD_MODEL.filename}")
    print(f"  engine={settings.engine} language={settings.language} mode={settings.mode} hotkey={settings.hotkey}")
    print("\nSetup:")
    line(not warnings, f"Settings {paths.config_path()}" + (f" ({len(warnings)} warning(s))" if warnings else ""))
    for warning in warnings:
        print(f"    {warning}")
    line(login_item.launcher_executable() is not None, f"App bundle {login_item.DEFAULT_APP}")
    line(paths.launcher_conf_path().exists(), f"Launcher config {paths.launcher_conf_path()}")
    print(f"  Start at login: {'on' if login_item.is_enabled() else 'off'}")
    print(f"  Logs: {paths.log_dir()}")
    print()
    return cmd_devices()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dikte", description="Menu-bar dictation for macOS (Turkish + English).")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="start the menu-bar app (default)")
    download = sub.add_parser("download", help="download a speech model")
    download.add_argument("model", nargs="?", choices=list(models.CATALOG))
    transcribe = sub.add_parser("transcribe", help="transcribe an audio file")
    transcribe.add_argument("file")
    transcribe.add_argument("--language", choices=LANGUAGES)
    sub.add_parser("devices", help="list microphones")
    sub.add_parser("doctor", help="check permissions, models and setup")
    args = parser.parse_args(argv)

    paths.ensure_dirs()
    if args.command == "download":
        return cmd_download(args.model)
    if args.command == "transcribe":
        return cmd_transcribe(args.file, args.language)
    if args.command == "devices":
        return cmd_devices()
    if args.command == "doctor":
        return cmd_doctor()
    from .app import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
