import numpy as np

from dikte.config import Settings, updated
from dikte.engines.base import EngineError, Transcript
from dikte.transcriber import Job, Transcriber


class FakeEngine:
    name = "fake"

    def __init__(self, text="Merhaba dünya", fail_start=False, fail_transcribe=False):
        self.text, self.fail_start, self.fail_transcribe = text, fail_start, fail_transcribe
        self.started = self.stopped = 0
        self.calls = []

    def start(self):
        self.started += 1
        if self.fail_start:
            raise EngineError("model missing")

    def stop(self):
        self.stopped += 1

    def transcribe(self, audio, language, prompt, repair=True):
        self.calls.append((language, prompt, repair))
        if self.fail_transcribe:
            raise EngineError("boom")
        return Transcript(text=self.text, language=language, duration_s=1.0, elapsed_s=0.5)


class FakeInserter:
    def __init__(self, allowed=True, pid=1):
        self.allowed, self.pid = allowed, pid
        self.inserted, self.copied = [], []

    def can_insert(self):
        return self.allowed

    def frontmost_pid(self):
        return self.pid

    def insert(self, text, settings, submit):
        self.inserted.append((text, submit))

    def copy(self, text):
        self.copied.append(text)


def make(engine=None, inserter=None):
    engine = engine or FakeEngine()
    inserter = inserter or FakeInserter()
    states = []
    worker = Transcriber(inserter, on_result=lambda r: None, on_engine_state=lambda ok, msg: states.append((ok, msg)),
                         engine_factory=lambda s: engine)
    return worker, engine, inserter, states


AUDIO = np.zeros(16000, np.float32)


def test_inserts_processed_text():
    worker, engine, inserter, states = make()
    worker._configure(Settings())
    result = worker._process(Job(AUDIO, Settings(), target_pid=1))
    assert inserter.inserted == [("Merhaba dünya", False)]
    assert result.text == "Merhaba dünya" and not result.copied_only
    assert states[-1] == (True, "")
    assert engine.calls[0][0] == "tr" and engine.calls[0][2] is True


def test_copies_when_accessibility_is_missing():
    worker, engine, inserter, _ = make(inserter=FakeInserter(allowed=False))
    result = worker._process(Job(AUDIO, Settings(), target_pid=1))
    assert inserter.inserted == [] and inserter.copied == ["Merhaba dünya"]
    assert result.copied_only and result.copy_reason == "no_permission"


def test_copies_when_app_changed():
    worker, engine, inserter, _ = make(inserter=FakeInserter(pid=2))
    result = worker._process(Job(AUDIO, Settings(), target_pid=1))
    assert inserter.copied == ["Merhaba dünya"]
    assert result.copy_reason == "app_changed"
    off = worker._process(Job(AUDIO, updated(Settings(), paste_guard=False), target_pid=1))
    assert not off.copied_only and inserter.inserted


def test_engine_errors_become_results():
    worker, _, inserter, states = make(engine=FakeEngine(fail_start=True))
    result = worker._process(Job(AUDIO, Settings()))
    assert result.error and inserter.inserted == []
    assert states[-1] == (False, "model missing")
    worker2, _, _, _ = make(engine=FakeEngine(fail_transcribe=True))
    assert "boom" in worker2._process(Job(AUDIO, Settings())).error


def test_hallucination_only_inserts_nothing():
    worker, _, inserter, _ = make(engine=FakeEngine(text="Altyazı M.K."))
    result = worker._process(Job(AUDIO, Settings()))
    assert result.text == "" and inserter.inserted == []


def test_submit_phrase_presses_enter_without_text():
    worker, _, inserter, _ = make(engine=FakeEngine(text="Gönder."))
    worker._process(Job(AUDIO, updated(Settings(), submit_phrases=["gönder"])))
    assert inserter.inserted == [("", True)]


def test_unexpected_start_failure_is_reported_and_cleaned_up():
    class Broken(FakeEngine):
        def start(self):
            self.started += 1
            raise OSError("Exec format error")

    engine = Broken()
    worker, _, _, states = make(engine=engine)
    worker._configure(Settings())
    assert states[-1] == (False, "Exec format error")
    assert engine.stopped == 1


def test_quit_while_model_loads_stops_the_engine():
    import threading

    loading, release = threading.Event(), threading.Event()

    class SlowStart(FakeEngine):
        def start(self):
            self.started += 1
            loading.set()
            release.wait(5)

    engine = SlowStart()
    worker, _, _, states = make(engine=engine)
    worker.start(Settings())
    assert loading.wait(5)
    worker.stop(timeout=0.2)  # worker is stuck in start(): stop() must reach the engine
    assert engine.stopped >= 1
    release.set()


def test_stop_drops_queued_jobs():
    worker, engine, inserter, _ = make()
    for _ in range(3):
        worker.submit(Job(AUDIO, Settings()))
    worker.stop()  # not started yet: the queued jobs must be discarded
    worker._run()  # process whatever is left, synchronously
    assert inserter.inserted == []


def test_voice_mode_never_presses_enter_and_keeps_every_word():
    worker, _, inserter, _ = make(engine=FakeEngine(text="Raporu yarın gönder."))
    worker._process(Job(AUDIO, updated(Settings(), mode="voice", submit_phrases=["gönder"], auto_enter=True)))
    assert inserter.inserted == [("Raporu yarın gönder.", False)]


def test_engine_is_reused_until_relevant_settings_change():
    worker, engine, _, _ = make()
    worker._configure(Settings())
    worker._configure(updated(Settings(), sounds=False))
    assert engine.started == 1
    worker._configure(updated(Settings(), local_model="large-v3-turbo-q8_0"))
    assert engine.started == 2 and engine.stopped == 1
