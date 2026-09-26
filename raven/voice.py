"""Local, offline speech-to-text for push-to-talk voice input (V1).

Fully local: sounddevice captures audio, faster-whisper transcribes it --
no network call, no OpenRouter quota consumed. (faster-whisper does need a
one-time download of its model weights from Hugging Face on first use,
cached locally afterward -- the same one-time-download shape Playwright's
own `playwright install chromium` already has, not a per-request cost.)

Both dependencies are OPTIONAL, not required to run R.A.V.E.N at all --
same posture as Gmail's google-api libraries (raven/gmail.py): imported
lazily, only once voice is actually used, so a plain `pip install -r
requirements.txt` install with an unplugged/absent mic still works for
everything else. is_available() lets callers check first and give a clear
"not set up" message instead of a traceback.

Transcription NEVER auto-submits what it hears -- it only returns text for
the caller (cli.py) to insert into the current input buffer for the user to
review, edit, or discard before pressing enter. Same posture as every other
consequential action in this project: the model doesn't get to act on your
behalf without you seeing it first.
"""
SAMPLE_RATE = 16000  # what faster-whisper expects; mono
DEFAULT_MODEL = "base"  # tiny/base/small/... -- "base" is the recommended
                         # default from this project's own earlier research
                         # note (accuracy/speed balance, CPU-friendly)

_whisper_model = None  # lazily constructed, reused across calls
_whisper_model_name = None  # which model_name _whisper_model was built with


class ModelNotDownloadedError(RuntimeError):
    """The Whisper model weights aren't in the local cache yet. Raised
    instead of downloading them -- see _get_model for why a keypress must
    never trigger a network download."""


def is_available() -> bool:
    """Whether the optional voice dependencies are usable. Doesn't check
    for an actual microphone device -- that's checked at record time, since
    a device can be plugged/unplugged between calls.

    Catches OSError as well as ImportError, deliberately: on Linux,
    `import sounddevice` raises OSError("PortAudio library not found") --
    not ImportError -- when the system library is missing. cli.py calls
    this on EVERY toolbar redraw, so an uncaught OSError here would break
    the whole bottom toolbar, not just voice."""
    try:
        import sounddevice  # noqa: F401
        import faster_whisper  # noqa: F401
    except (ImportError, OSError):
        return False
    return True


def model_ready(model_name: str = DEFAULT_MODEL) -> bool:
    """Whether the model weights are fully present in the local cache.
    Purely a local disk check (local_files_only=True) -- never touches the
    network, so it's safe to call from anywhere, including a key handler."""
    try:
        from faster_whisper.utils import download_model
        download_model(model_name, local_files_only=True)
    except Exception:
        return False
    return True


def download_model(model_name: str = DEFAULT_MODEL) -> str:
    """Fetch the model weights into the local cache (network, ~140MB for
    "base"). The ONLY place that's allowed to download -- run it explicitly
    (`python -m raven.voice`) rather than as a side effect of pressing a
    key. Resumable: re-running picks up where the cache left off."""
    from faster_whisper.utils import download_model as _download
    return _download(model_name)


def _get_model(model_name: str = DEFAULT_MODEL):
    """Load (and cache) the Whisper model from the LOCAL cache only.

    local_files_only=True is load-bearing: without it, faster-whisper
    downloads any missing weights on construction. Called from a
    prompt_toolkit key handler, that used to freeze the entire UI --
    Ctrl+C and Ctrl+V included -- for as long as the download took, and on
    a slow or blocked connection the download made no progress at all
    (found live: four `model.bin.incomplete` files, all 0 bytes). A
    keypress must never be able to do that."""
    global _whisper_model, _whisper_model_name
    if _whisper_model is None or _whisper_model_name != model_name:
        from faster_whisper import WhisperModel
        try:
            _whisper_model = WhisperModel(
                model_name, device="cpu", compute_type="int8", local_files_only=True,
            )
        except Exception as e:
            raise ModelNotDownloadedError(
                f"Whisper model '{model_name}' isn't downloaded yet -- run: python -m raven.voice"
            ) from e
        _whisper_model_name = model_name
    return _whisper_model


class Recorder:
    """Push-to-talk audio capture: start() begins streaming capture into an
    in-memory buffer (no temp file, nothing written to disk), stop() ends it
    and returns the raw float32 samples. One Recorder instance is meant to
    be reused across start()/stop() cycles (cli.py keeps one alive for the
    session), not recreated per recording."""

    def __init__(self, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self._stream = None
        self._chunks: list = []

    def start(self) -> None:
        if self._stream is not None:
            return  # already recording -- idempotent, not an error
        import sounddevice as sd
        self._chunks = []

        def callback(indata, frames, time_info, status):
            self._chunks.append(indata.copy())

        self._stream = sd.InputStream(
            samplerate=self.sample_rate, channels=1, dtype="float32", callback=callback,
        )
        self._stream.start()

    def stop(self):
        """Stop capture and return the recorded audio as a 1-D float32
        numpy array (empty array if nothing was captured, e.g. start()
        was never called or no audio arrived before stop())."""
        import numpy as np
        if self._stream is None:
            return np.array([], dtype="float32")
        self._stream.stop()
        self._stream.close()
        self._stream = None
        if not self._chunks:
            return np.array([], dtype="float32")
        return np.concatenate(self._chunks, axis=0).reshape(-1)

    @property
    def is_recording(self) -> bool:
        return self._stream is not None


def transcribe(audio, model_name: str = DEFAULT_MODEL) -> str:
    """Transcribe a 1-D float32 numpy array of audio (as returned by
    Recorder.stop()) at SAMPLE_RATE. Returns "" for empty/silent audio
    rather than raising -- a push-to-talk toggle pressed and released
    immediately with nothing said is a normal outcome, not an error."""
    if audio is None or len(audio) == 0:
        return ""
    model = _get_model(model_name)
    segments, _info = model.transcribe(audio, language=None, vad_filter=True)
    return " ".join(seg.text.strip() for seg in segments).strip()


if __name__ == "__main__":
    import sys
    name = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MODEL
    if not is_available():
        sys.exit("Voice dependencies aren't usable -- pip install sounddevice faster-whisper "
                 "(and on Linux: sudo apt install libportaudio2)")
    if model_ready(name):
        print(f"Model '{name}' is already downloaded.")
    else:
        print(f"Downloading Whisper model '{name}' (re-run this command to resume if interrupted)...")
        print(f"Saved to {download_model(name)}")
