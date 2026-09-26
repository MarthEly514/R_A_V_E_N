"""raven/voice.py — push-to-talk local transcription (V1).

Uses fake modules injected into sys.modules (monkeypatch.setitem) rather
than the real sounddevice/faster-whisper packages, so these tests are fast,
deterministic, need no real microphone or model download, and work whether
or not the real optional packages happen to be installed in this
environment -- sys.modules is checked before the real package either way.
"""
import sys
import types

import pytest

from raven import voice


# ---------------------------------------------------------------------------
# is_available()
# ---------------------------------------------------------------------------

def test_is_available_false_when_a_dependency_is_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)  # None -> import raises ImportError
    assert voice.is_available() is False


def test_is_available_true_when_both_present(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", types.ModuleType("sounddevice"))
    monkeypatch.setitem(sys.modules, "faster_whisper", types.ModuleType("faster_whisper"))
    assert voice.is_available() is True


# ---------------------------------------------------------------------------
# Recorder: start()/stop() against a fake sounddevice.InputStream
# ---------------------------------------------------------------------------

class FakeInputStream:
    """Stands in for sounddevice.InputStream -- records the callback it was
    given so a test can simulate audio chunks arriving, and tracks
    start()/stop()/close() calls."""
    instances = []

    def __init__(self, samplerate, channels, dtype, callback):
        self.samplerate = samplerate
        self.callback = callback
        self.started = False
        self.stopped = False
        self.closed = False
        FakeInputStream.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


@pytest.fixture
def fake_sounddevice(monkeypatch):
    import numpy as np
    FakeInputStream.instances = []
    fake = types.ModuleType("sounddevice")
    fake.InputStream = FakeInputStream
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    return fake


def test_recorder_start_opens_a_stream(fake_sounddevice):
    r = voice.Recorder()
    r.start()
    assert len(FakeInputStream.instances) == 1
    assert FakeInputStream.instances[0].started is True
    assert r.is_recording is True


def test_recorder_start_is_idempotent(fake_sounddevice):
    """A second start() while already recording must not open a second
    stream -- e.g. a rapid double key-press must not leak a stream."""
    r = voice.Recorder()
    r.start()
    r.start()
    assert len(FakeInputStream.instances) == 1


def test_recorder_stop_without_start_returns_empty(fake_sounddevice):
    r = voice.Recorder()
    result = r.stop()
    assert len(result) == 0


def test_recorder_stop_concatenates_captured_chunks(fake_sounddevice):
    import numpy as np
    r = voice.Recorder()
    r.start()
    stream = FakeInputStream.instances[0]
    # Simulate two audio chunks arriving via the callback, as sounddevice would.
    stream.callback(np.array([[0.1], [0.2]], dtype="float32"), 2, None, None)
    stream.callback(np.array([[0.3]], dtype="float32"), 1, None, None)
    result = r.stop()
    assert list(result) == pytest.approx([0.1, 0.2, 0.3])
    assert stream.stopped is True
    assert stream.closed is True
    assert r.is_recording is False


def test_recorder_stop_clears_the_stream_so_a_new_start_works(fake_sounddevice):
    r = voice.Recorder()
    r.start()
    r.stop()
    r.start()
    assert len(FakeInputStream.instances) == 2  # a genuinely new stream, not reusing a closed one


# ---------------------------------------------------------------------------
# transcribe()
# ---------------------------------------------------------------------------

class _FakeSegment:
    def __init__(self, text):
        self.text = text


class FakeWhisperModel:
    """Stands in for faster_whisper.WhisperModel."""
    instances = []

    def __init__(self, model_name, device, compute_type, local_files_only=False):
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.local_files_only = local_files_only
        FakeWhisperModel.instances.append(self)

    def transcribe(self, audio, language=None, vad_filter=True):
        return [_FakeSegment(" hello "), _FakeSegment("world ")], {"language": "en"}


@pytest.fixture
def fake_faster_whisper(monkeypatch):
    import numpy as np
    FakeWhisperModel.instances = []
    fake = types.ModuleType("faster_whisper")
    fake.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    monkeypatch.setattr(voice, "_whisper_model", None)
    monkeypatch.setattr(voice, "_whisper_model_name", None)
    return fake


def test_transcribe_empty_audio_is_empty_string(fake_faster_whisper):
    import numpy as np
    assert voice.transcribe(np.array([], dtype="float32")) == ""


def test_transcribe_none_audio_is_empty_string(fake_faster_whisper):
    assert voice.transcribe(None) == ""


def test_transcribe_joins_segment_text(fake_faster_whisper):
    import numpy as np
    result = voice.transcribe(np.array([0.1, 0.2], dtype="float32"))
    assert result == "hello world"


def test_transcribe_reuses_the_model_across_calls(fake_faster_whisper):
    import numpy as np
    audio = np.array([0.1], dtype="float32")
    voice.transcribe(audio)
    voice.transcribe(audio)
    assert len(FakeWhisperModel.instances) == 1  # not rebuilt every call


def test_transcribe_rebuilds_the_model_if_a_different_model_name_is_requested(fake_faster_whisper):
    import numpy as np
    audio = np.array([0.1], dtype="float32")
    voice.transcribe(audio, model_name="tiny")
    voice.transcribe(audio, model_name="small")
    assert len(FakeWhisperModel.instances) == 2


# ---------------------------------------------------------------------------
# Regressions found live (2026-09-25): voice froze the whole UI on every
# Ctrl+V because the Whisper model was downloaded from inside a key handler.
# ---------------------------------------------------------------------------

def test_is_available_false_when_portaudio_is_missing_oserror(monkeypatch):
    """On Linux `import sounddevice` raises OSError -- NOT ImportError --
    when libportaudio2 is missing. is_available() runs on every toolbar
    redraw, so letting that propagate would break the whole toolbar."""
    class Boom:
        def __getattr__(self, name):
            raise OSError("PortAudio library not found")

    def fake_import(name, *a, **k):
        if name == "sounddevice":
            raise OSError("PortAudio library not found")
        return real_import(name, *a, **k)

    import builtins
    real_import = builtins.__import__
    monkeypatch.delitem(sys.modules, "sounddevice", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert voice.is_available() is False


def test_model_ready_true_when_the_local_cache_is_complete(monkeypatch):
    utils = types.ModuleType("faster_whisper.utils")
    calls = []
    utils.download_model = lambda name, local_files_only=False: calls.append(local_files_only) or "/cache"
    monkeypatch.setitem(sys.modules, "faster_whisper.utils", utils)
    assert voice.model_ready("base") is True
    assert calls == [True]  # local-only: never asked to touch the network


def test_model_ready_false_when_the_cache_is_incomplete(monkeypatch):
    utils = types.ModuleType("faster_whisper.utils")

    def incomplete(name, local_files_only=False):
        raise RuntimeError("cached snapshot is incomplete: model.bin missing")

    utils.download_model = incomplete
    monkeypatch.setitem(sys.modules, "faster_whisper.utils", utils)
    assert voice.model_ready("base") is False


def test_get_model_loads_with_local_files_only(fake_faster_whisper):
    """The core fix: constructing the model must never be allowed to
    download -- that's what froze the UI for hours."""
    seen = {}

    class Capturing(FakeWhisperModel):
        def __init__(self, model_name, device, compute_type, local_files_only=False):
            seen["local_files_only"] = local_files_only
            super().__init__(model_name, device, compute_type)

    fake_faster_whisper.WhisperModel = Capturing
    voice._get_model("base")
    assert seen["local_files_only"] is True


def test_get_model_raises_a_clear_error_when_the_model_is_not_downloaded(fake_faster_whisper):
    class Missing:
        def __init__(self, *a, **k):
            raise RuntimeError("cached snapshot is incomplete")

    fake_faster_whisper.WhisperModel = Missing
    with pytest.raises(voice.ModelNotDownloadedError, match="python -m raven.voice"):
        voice._get_model("base")


def test_transcribe_surfaces_a_missing_model_rather_than_downloading(fake_faster_whisper):
    import numpy as np

    class Missing:
        def __init__(self, *a, **k):
            raise RuntimeError("incomplete")

    fake_faster_whisper.WhisperModel = Missing
    with pytest.raises(voice.ModelNotDownloadedError):
        voice.transcribe(np.array([0.1], dtype="float32"))
