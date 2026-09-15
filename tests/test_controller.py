import numpy as np
import pytest

from dikte.audio import AudioError
from dikte.config import Settings, updated
from dikte.controller import Controller
from dikte.levels import FRAME_S, FRAME_SAMPLES, SAMPLE_RATE
from dikte.transcriber import JobResult

RNG = np.random.default_rng(3)


def noise(seconds, db=-70.0):
    return (RNG.standard_normal(int(seconds * SAMPLE_RATE)) * 10 ** (db / 20)).astype(np.float32)


def tone(seconds, db=-20.0):
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    return (10 ** (db / 20) * np.sqrt(2) * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class FakeAudio:
    def __init__(self, fail=False):
        self.is_open = False
        self.device_name = ""
        self.fail = fail
        self.opens = 0

    def open(self, preferred):
        if self.fail:
            raise AudioError("No microphone found.")
        self.opens += 1
        self.is_open, self.device_name = True, "USB Microphone"
        return self.device_name

    def close(self):
        self.is_open, self.device_name = False, ""

    def rescan(self):
        pass


class FakeWorker:
    def __init__(self):
        self.jobs, self.inserted, self.configured = [], [], []

    def configure(self, settings):
        self.configured.append(settings)

    def submit(self, job):
        self.jobs.append(job)

    def insert_text(self, text, settings):
        self.inserted.append(text)


class FakeUi:
    def __init__(self):
        self.statuses, self.sounds, self.notes, self.history, self.thresholds = [], [], [], [], []

    def status_changed(self, status):
        self.statuses.append(status)

    def play(self, sound):
        self.sounds.append(sound)

    def notify(self, message):
        self.notes.append(message)

    def history_changed(self, items):
        self.history.append(items)

    def calibrated(self, threshold_db):
        self.thresholds.append(threshold_db)


@pytest.fixture
def rig():
    def build(settings=None, audio=None):
        clock, ui, worker = Clock(), FakeUi(), FakeWorker()
        audio = audio or FakeAudio()
        ctrl = Controller(settings or Settings(), audio, worker, lambda: 4242, ui, clock=clock)
        ctrl.engine_state(True, "")
        ctrl.run_pending()
        return ctrl, clock, audio, worker, ui

    return build


def feed(ctrl, clock, audio):
    for i in range(0, len(audio) - FRAME_SAMPLES + 1, FRAME_SAMPLES):
        clock.t += FRAME_S
        ctrl.push_frame(audio[i:i + FRAME_SAMPLES])
        ctrl.run_pending()


def key(ctrl, clock, kind):
    ctrl.hotkey(kind, clock.t)
    ctrl.run_pending()


def test_push_to_talk_submits_one_job(rig):
    ctrl, clock, audio, worker, ui = rig()
    key(ctrl, clock, "down")
    assert audio.is_open
    feed(ctrl, clock, tone(1.5))
    assert ui.sounds == ["start"]
    assert ui.statuses[-1].phase == "recording"
    key(ctrl, clock, "up")
    feed(ctrl, clock, noise(0.3))  # release tail
    assert len(worker.jobs) == 1
    job = worker.jobs[0]
    assert 1.5 <= len(job.audio) / SAMPLE_RATE <= 1.8
    assert job.target_pid == 4242
    assert ui.sounds == ["start", "stop"]
    assert ui.statuses[-1].phase == "transcribing"


def test_quick_tap_is_silent_and_submits_nothing(rig):
    ctrl, clock, audio, worker, ui = rig()
    key(ctrl, clock, "down")
    feed(ctrl, clock, tone(0.1))
    key(ctrl, clock, "up")
    feed(ctrl, clock, noise(0.5))
    assert worker.jobs == [] and ui.sounds == []


def test_shortcut_with_hotkey_cancels(rig):
    ctrl, clock, audio, worker, ui = rig()
    key(ctrl, clock, "down")
    feed(ctrl, clock, tone(0.05))
    key(ctrl, clock, "other")
    feed(ctrl, clock, tone(1.0))
    key(ctrl, clock, "up")
    feed(ctrl, clock, noise(0.3))
    assert worker.jobs == [] and ui.sounds == []


def test_muted_mic_is_reported_not_transcribed(rig):
    ctrl, clock, audio, worker, ui = rig()
    key(ctrl, clock, "down")
    feed(ctrl, clock, np.zeros(SAMPLE_RATE, np.float32))
    key(ctrl, clock, "up")
    feed(ctrl, clock, np.zeros(SAMPLE_RATE // 2, np.float32))
    assert worker.jobs == []
    assert any("muted" in n for n in ui.notes)


def test_hands_free_stops_on_silence(rig):
    ctrl, clock, audio, worker, ui = rig(updated(Settings(), mode="toggle", silence_stop_s=1.0))
    key(ctrl, clock, "down")
    feed(ctrl, clock, noise(0.05))
    key(ctrl, clock, "up")
    assert ui.statuses[-1].hands_free
    feed(ctrl, clock, np.concatenate([noise(0.5), tone(1.0), noise(1.5)]))
    assert len(worker.jobs) == 1
    assert not ui.statuses[-1].hands_free


def test_hands_free_times_out_without_speech(rig):
    ctrl, clock, audio, worker, ui = rig(updated(Settings(), mode="toggle", no_speech_timeout_s=1.0))
    key(ctrl, clock, "down")
    key(ctrl, clock, "up")
    feed(ctrl, clock, noise(1.5))
    assert worker.jobs == []
    assert ui.sounds[-1] == "cancel"


def test_escape_cancels_recording(rig):
    ctrl, clock, audio, worker, ui = rig()
    key(ctrl, clock, "down")
    feed(ctrl, clock, tone(1.0))
    key(ctrl, clock, "escape")
    key(ctrl, clock, "up")
    feed(ctrl, clock, noise(0.5))
    assert worker.jobs == []
    assert ui.sounds == ["start", "cancel"]


def test_voice_mode_detects_utterance(rig):
    ctrl, clock, audio, worker, ui = rig(updated(Settings(), mode="voice", voice_silence_stop_s=0.6))
    assert audio.is_open  # always listening
    feed(ctrl, clock, np.concatenate([noise(1.5), tone(1.0), noise(1.0)]))
    assert len(worker.jobs) == 1
    assert worker.jobs[0].target_pid == 4242
    key(ctrl, clock, "down")  # tap pauses listening
    key(ctrl, clock, "up")
    feed(ctrl, clock, np.concatenate([noise(0.5), tone(1.0), noise(1.0)]))
    assert len(worker.jobs) == 1


def test_escape_drops_voice_utterance(rig):
    ctrl, clock, audio, worker, ui = rig(updated(Settings(), mode="voice", voice_silence_stop_s=0.6))
    feed(ctrl, clock, np.concatenate([noise(1.5), tone(0.6)]))
    assert ui.statuses[-1].phase == "recording"
    key(ctrl, clock, "escape")
    feed(ctrl, clock, noise(1.0))
    assert worker.jobs == []
    assert ui.statuses[-1].phase == "listening"


def test_mode_switch_mid_utterance_does_not_stick_in_recording(rig):
    ctrl, clock, audio, worker, ui = rig(updated(Settings(), mode="voice"))
    feed(ctrl, clock, np.concatenate([noise(1.5), tone(0.6)]))
    assert ui.statuses[-1].phase == "recording"
    ctrl.command("settings", updated(Settings(), mode="hold"))
    ctrl.run_pending()
    assert ui.statuses[-1].phase == "idle"


def test_mic_retry_beeps_only_once(rig):
    ctrl, clock, audio, worker, ui = rig(updated(Settings(), mode="voice"), audio=FakeAudio(fail=True))
    for _ in range(4):
        clock.t += 6.0
        ctrl.run_pending()
    assert ui.sounds.count("error") == 1
    assert ui.notes == ["No microphone found."]
    assert ui.statuses[-1].phase == "error"


def test_device_change_during_recording_applies_afterwards(rig):
    ctrl, clock, audio, worker, ui = rig()
    key(ctrl, clock, "down")
    feed(ctrl, clock, tone(0.5))
    ctrl.command("settings", updated(Settings(), input_device="USB Mic"))
    ctrl.run_pending()
    assert audio.is_open and audio.opens == 1  # untouched mid-recording
    key(ctrl, clock, "up")
    feed(ctrl, clock, noise(0.3))
    assert len(worker.jobs) == 1
    clock.t += 0.1
    ctrl.run_pending()
    assert audio.opens == 1 and not audio.is_open  # closed; next open uses the new device


def test_push_to_talk_keeps_speech_voice_mode_already_heard(rig):
    ctrl, clock, audio, worker, ui = rig(updated(Settings(), mode="voice"))
    feed(ctrl, clock, np.concatenate([noise(1.5), tone(1.5)]))
    key(ctrl, clock, "down")
    feed(ctrl, clock, tone(0.5))
    key(ctrl, clock, "up")
    feed(ctrl, clock, noise(0.3))
    assert len(worker.jobs) == 1
    assert len(worker.jobs[0].audio) / SAMPLE_RATE > 2.0  # the 1.5 s before the key press is kept


def test_every_failed_press_is_reported_but_retries_are_quiet(rig):
    ctrl, clock, audio, worker, ui = rig(audio=FakeAudio(fail=True))
    for _ in range(3):
        key(ctrl, clock, "down")
        key(ctrl, clock, "up")
        clock.t += 1.0
    assert ui.sounds.count("error") == 3  # the user pressed three times: three answers


def test_max_length_during_voice_handover_still_submits(rig):
    ctrl, clock, audio, worker, ui = rig(updated(Settings(), mode="voice", max_recording_s=5))
    feed(ctrl, clock, np.concatenate([noise(1.5), tone(4.5)]))
    assert worker.jobs == [] and ui.statuses[-1].phase == "recording"  # ~4.9 s heard so far
    key(ctrl, clock, "down")
    feed(ctrl, clock, tone(0.15))  # crosses 5 s before the 0.2 s hold threshold
    assert len(worker.jobs) == 1
    assert len(worker.jobs[0].audio) / SAMPLE_RATE >= 4.9


def test_mic_failure_is_reported(rig):
    ctrl, clock, audio, worker, ui = rig(audio=FakeAudio(fail=True))
    key(ctrl, clock, "down")
    clock.t += 0.3
    ctrl.run_pending()
    key(ctrl, clock, "up")
    assert worker.jobs == []
    assert ui.notes == ["No microphone found."]


def test_result_updates_history_and_message(rig):
    ctrl, clock, audio, worker, ui = rig()
    ctrl.job_finished(JobResult(text="Merhaba dünya", elapsed_s=0.9))
    ctrl.run_pending()
    assert ui.history[-1] == ("Merhaba dünya",)
    assert ui.statuses[-1].message.startswith("Inserted 2 words")


def test_calibration_sets_threshold(rig):
    ctrl, clock, audio, worker, ui = rig()
    ctrl.command("calibrate")
    ctrl.run_pending()
    feed(ctrl, clock, noise(3.0, db=-55))
    assert len(ui.thresholds) == 1
    assert -47 < ui.thresholds[0] < -43  # -55 dB room + 10 dB margin


def test_pre_roll_does_not_leak_between_recordings(rig):
    ctrl, clock, audio, worker, ui = rig()
    for _ in range(2):
        key(ctrl, clock, "down")
        feed(ctrl, clock, tone(1.0))
        key(ctrl, clock, "up")
        feed(ctrl, clock, noise(0.5))
    assert len(worker.jobs) == 2
    assert len(worker.jobs[1].audio) / SAMPLE_RATE < 1.6
