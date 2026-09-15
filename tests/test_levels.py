import numpy as np

from dikte.levels import (
    FRAME_S,
    FRAME_SAMPLES,
    SAMPLE_RATE,
    GateParams,
    LevelGate,
    SilenceWatcher,
    UtteranceDetector,
    VoiceParams,
    level_db,
    peak_db,
    speech_regions,
)

RNG = np.random.default_rng(7)


def noise(seconds: float, db: float) -> np.ndarray:
    amp = 10 ** (db / 20)
    return (RNG.standard_normal(int(seconds * SAMPLE_RATE)) * amp).astype(np.float32)


def tone(seconds: float, db: float, hz: float = 220.0) -> np.ndarray:
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    amp = 10 ** (db / 20) * np.sqrt(2)
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def frames(audio: np.ndarray):
    for i in range(0, len(audio) - FRAME_SAMPLES + 1, FRAME_SAMPLES):
        yield audio[i : i + FRAME_SAMPLES]


def run(detector, audio):
    events = []
    for frame in frames(audio):
        events.extend(detector.feed(frame))
    return events


def test_level_and_peak():
    assert abs(level_db(tone(0.1, -20)) - -20) < 0.5
    assert level_db(np.zeros(512, np.float32)) == -100.0
    assert peak_db(np.zeros(10, np.float32)) == -100.0


def test_auto_threshold_follows_noise_but_not_below_minimum():
    gate = LevelGate(GateParams(auto=True, margin_db=10, threshold_db=-50))
    for frame in frames(noise(3, -40)):
        gate.update(frame)
    assert -32 < gate.threshold < -28
    quiet = LevelGate(GateParams(auto=True, margin_db=10, threshold_db=-50))
    for frame in frames(noise(3, -80)):
        quiet.update(frame)
    assert quiet.threshold == -50


def test_utterance_detected_with_pre_roll():
    detector = UtteranceDetector(LevelGate(GateParams()), VoiceParams(silence_stop_s=0.5))
    audio = np.concatenate([noise(1.5, -70), tone(1.0, -20), noise(1.0, -70)])
    events = run(detector, audio)
    kinds = [e[0] for e in events]
    assert kinds == ["start", "end"]
    clip = events[1][1]
    assert 1.1 < len(clip) / SAMPLE_RATE < 1.9  # speech + pre-roll + tail


def test_keyboard_clicks_do_not_trigger():
    detector = UtteranceDetector(LevelGate(GateParams()), VoiceParams())
    clicks = [noise(0.9, -70)]
    for _ in range(12):  # ~6 clicks per second, 15 ms each
        clicks += [noise(0.015, -15), noise(0.15, -70)]
    assert run(detector, np.concatenate(clicks)) == []


def test_short_blip_is_discarded():
    detector = UtteranceDetector(LevelGate(GateParams()), VoiceParams(min_speech_s=0.6, silence_stop_s=0.4))
    audio = np.concatenate([noise(1.5, -70), tone(0.35, -20), noise(1.0, -70)])
    assert [e[0] for e in run(detector, audio)] == ["start", "discard"]


def test_max_utterance_splits_and_keeps_listening():
    detector = UtteranceDetector(LevelGate(GateParams()), VoiceParams(max_utterance_s=1.0, silence_stop_s=0.5))
    audio = np.concatenate([noise(1.5, -70), tone(2.3, -20), noise(1.0, -70)])
    kinds = [e[0] for e in run(detector, audio)]
    assert kinds[:3] == ["start", "end", "start"]
    assert kinds[-1] == "end"


def test_silence_watcher_stops_after_speech():
    watcher = SilenceWatcher(LevelGate(GateParams()), silence_stop_s=0.8, no_speech_timeout_s=5)
    verdicts = [watcher.feed(f) for f in frames(np.concatenate([noise(1.0, -70), tone(1.0, -20), noise(1.5, -70)]))]
    stop_at = verdicts.index("stop") * FRAME_S
    assert 2.7 < stop_at < 3.0


def test_silence_watcher_times_out_without_speech():
    watcher = SilenceWatcher(LevelGate(GateParams()), silence_stop_s=0.8, no_speech_timeout_s=1.0)
    verdicts = [watcher.feed(f) for f in frames(noise(2.0, -70))]
    assert "timeout" in verdicts and "stop" not in verdicts


def test_speech_regions():
    audio = np.concatenate([noise(1.0, -70), tone(1.0, -20), noise(1.0, -70), tone(0.8, -25), noise(0.5, -70)])
    regions = speech_regions(audio)
    assert len(regions) == 2
    assert abs(regions[0][0] - 1.0) < 0.1 and abs(regions[1][1] - 3.8) < 0.1
