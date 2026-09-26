"""raven/cli.py — the pieces that don't need a real terminal.

Excludes main()/PromptSession/the input loop, which need a real TTY. Every
function here is pure or takes its dependencies (status, console) as
parameters/mocks.
"""
import time
from unittest.mock import patch

import pytest
from rich.console import Console

import raven.cli as cli
from raven import tools
from raven.assistant import Assistant


# ---------------------------------------------------------------------------
# TOOL_STATUS: every registered tool has a status phrase (the class of bug
# that would silently leave the spinner on "Using <tool>..." for a new tool)
# ---------------------------------------------------------------------------

def test_every_tool_has_a_status_phrase():
    missing = [n for n in tools.TOOL_FUNCTIONS if n not in cli.TOOL_STATUS]
    assert missing == []


def test_unregistered_tool_falls_back_to_a_generic_phrase():
    assert cli.tool_status_text("some_future_tool", {}) == "Using some_future_tool..."


def test_list_dir_status_current_directory_is_readable():
    assert cli.tool_status_text("list_dir", {}) == "Looking into the current directory..."
    assert cli.tool_status_text("list_dir", {"path": "."}) == "Looking into the current directory..."
    assert cli.tool_status_text("list_dir", {"path": "raven"}) == "Looking into raven..."


def test_load_skill_status_phrase():
    assert cli.tool_status_text("load_skill", {"name": "gmail"}) == "Loading skill: gmail..."


# ---------------------------------------------------------------------------
# render_thought_line / reasoning_toolbar_text (ctrl+o live toggle, Step 3.16)
# ---------------------------------------------------------------------------

def test_render_thought_line_no_reasoning_has_no_hint():
    cli.console = Console(record=True, width=100)
    cli.render_thought_line(3.1, has_reasoning=False)
    text = cli.console.export_text()
    assert "Thought for 3.1s" in text and "ctrl+o" not in text


def test_render_thought_line_with_reasoning_hints_at_ctrl_o():
    cli.console = Console(record=True, width=100)
    cli.render_thought_line(3.1, has_reasoning=True)
    assert "ctrl+o to view" in cli.console.export_text()


def test_reasoning_toolbar_collapsed_or_empty_is_blank():
    assert cli.reasoning_toolbar_text([], expanded=True) == ""
    assert cli.reasoning_toolbar_text(["x"], expanded=False) == ""


def test_reasoning_toolbar_expanded_shows_the_trace():
    out = cli.reasoning_toolbar_text(["step one", "step two"], expanded=True)
    assert out == "\nstep one\nstep two"


def test_reasoning_toolbar_truncates_a_long_trace():
    out = cli.reasoning_toolbar_text(["x" * 2000], expanded=True)
    assert "truncated" in out
    assert len(out) < 1300


def test_toggling_the_same_reasoning_changes_only_the_toolbar_not_the_data():
    """The point of Step 3.16: ctrl+o must change what's SHOWN for the same
    already-completed reply, with no new ask() call — proven here by calling
    reasoning_toolbar_text on the same data with expanded flipped."""
    reasoning = ["secret reasoning"]
    assert cli.reasoning_toolbar_text(reasoning, expanded=False) == ""
    assert "secret reasoning" in cli.reasoning_toolbar_text(reasoning, expanded=True)


def test_ctrl_o_handler_toggles_ui_state():
    cli.UI_STATE["show_thoughts"] = False
    cli._toggle_thoughts(event=None)
    assert cli.UI_STATE["show_thoughts"] is True
    cli._toggle_thoughts(event=None)
    assert cli.UI_STATE["show_thoughts"] is False


# ---------------------------------------------------------------------------
# Voice input (V1): toolbar hint + the ctrl+v toggle handler
# ---------------------------------------------------------------------------

def test_voice_toolbar_hint_blank_when_not_available():
    with patch.object(cli.voice, "is_available", return_value=False):
        assert cli.voice_toolbar_hint(recording=False) == ""
        assert cli.voice_toolbar_hint(recording=True) == ""


def test_voice_toolbar_hint_shows_idle_and_recording_states():
    with patch.object(cli.voice, "is_available", return_value=True):
        idle = cli.voice_toolbar_hint(recording=False)
        active = cli.voice_toolbar_hint(recording=True)
        assert "ctrl+v" in idle and "recording" not in idle.lower()
        assert "recording" in active.lower()
        assert idle != active


class _FakeBuffer:
    def __init__(self):
        self.inserted = []

    def insert_text(self, text):
        self.inserted.append(text)


class _FakeLoop:
    """call_soon_threadsafe just runs the callback -- stands in for the
    event loop so the background worker's result can be asserted on."""
    def call_soon_threadsafe(self, fn):
        fn()


class _FakeApp:
    def __init__(self):
        self.invalidated = 0
        self.loop = _FakeLoop()

    def invalidate(self):
        self.invalidated += 1


class _FakeKeyEvent:
    def __init__(self):
        self.current_buffer = _FakeBuffer()
        self.app = _FakeApp()


class _InlineThread:
    """Runs the target immediately instead of on a real thread, so the test
    can assert on the result deterministically. Records that a thread WAS
    used -- the point of the fix is that transcription is off the UI thread."""
    started = []

    def __init__(self, target, args=(), daemon=None):
        self._target, self._args = target, args
        _InlineThread.started.append(target)

    def start(self):
        self._target(*self._args)


@pytest.fixture(autouse=True)
def _reset_voice_ui_state():
    cli.UI_STATE["recording"] = False
    cli.UI_STATE["voice_status"] = ""
    _InlineThread.started = []
    yield
    cli.UI_STATE["recording"] = False
    cli.UI_STATE["voice_status"] = ""


def test_voice_toolbar_hint_shows_transcribing_and_error_status():
    with patch.object(cli.voice, "is_available", return_value=True):
        assert "transcribing" in cli.voice_toolbar_hint(False, "transcribing")
        assert "not downloaded" in cli.voice_toolbar_hint(False, "voice model not downloaded")
        # recording wins over a stale status
        assert "recording" in cli.voice_toolbar_hint(True, "transcribing")


def test_toggle_recording_when_not_available_inserts_a_hint_and_does_not_record():
    event = _FakeKeyEvent()
    with patch.object(cli.voice, "is_available", return_value=False):
        cli._toggle_recording(event)
    assert cli.UI_STATE["recording"] is False
    assert "not set up" in event.current_buffer.inserted[0]


def test_toggle_recording_refuses_to_start_when_the_model_is_not_downloaded():
    """Regression: the model used to be downloaded from INSIDE this key
    handler, freezing the whole UI (Ctrl+C included) -- and on a bad
    connection the download never finished at all. Now a missing model is
    reported, never fetched, and recording doesn't even start."""
    event = _FakeKeyEvent()
    with patch.object(cli.voice, "is_available", return_value=True), \
         patch.object(cli.voice, "model_ready", return_value=False), \
         patch.object(cli, "VOICE_RECORDER") as mock_recorder:
        cli._toggle_recording(event)
    mock_recorder.start.assert_not_called()
    assert cli.UI_STATE["recording"] is False
    assert "python -m raven.voice" in cli.UI_STATE["voice_status"]


def test_toggle_recording_starts_then_stops_and_inserts_transcribed_text():
    start_event, stop_event = _FakeKeyEvent(), _FakeKeyEvent()
    with patch.object(cli.voice, "is_available", return_value=True), \
         patch.object(cli.voice, "model_ready", return_value=True), \
         patch.object(cli, "VOICE_RECORDER") as mock_recorder, \
         patch.object(cli.threading, "Thread", _InlineThread), \
         patch.object(cli.voice, "transcribe", return_value="hello world"):
        cli._toggle_recording(start_event)
        assert cli.UI_STATE["recording"] is True
        mock_recorder.start.assert_called_once()
        assert start_event.current_buffer.inserted == []

        cli._toggle_recording(stop_event)
    assert cli.UI_STATE["recording"] is False
    mock_recorder.stop.assert_called_once()
    assert stop_event.current_buffer.inserted == ["hello world"]
    assert cli.UI_STATE["voice_status"] == ""  # "transcribing" cleared on completion


def test_toggle_recording_transcribes_on_a_background_thread_not_inline():
    """The other half of the freeze fix: transcription (model load +
    inference, seconds long) must go through a thread, never run inline on
    the event-loop thread that a key handler executes on."""
    cli.UI_STATE["recording"] = True
    event = _FakeKeyEvent()
    with patch.object(cli.voice, "is_available", return_value=True), \
         patch.object(cli, "VOICE_RECORDER"), \
         patch.object(cli.threading, "Thread", _InlineThread), \
         patch.object(cli.voice, "transcribe", return_value="x"):
        cli._toggle_recording(event)
    assert _InlineThread.started == [cli._run_transcription]


def test_toggle_recording_ignores_a_press_while_still_transcribing():
    cli.UI_STATE["voice_status"] = "transcribing"
    event = _FakeKeyEvent()
    with patch.object(cli.voice, "is_available", return_value=True), \
         patch.object(cli, "VOICE_RECORDER") as mock_recorder:
        cli._toggle_recording(event)
    mock_recorder.start.assert_not_called()


def test_toggle_recording_empty_transcription_inserts_nothing():
    cli.UI_STATE["recording"] = True
    event = _FakeKeyEvent()
    with patch.object(cli.voice, "is_available", return_value=True), \
         patch.object(cli, "VOICE_RECORDER"), \
         patch.object(cli.threading, "Thread", _InlineThread), \
         patch.object(cli.voice, "transcribe", return_value=""):
        cli._toggle_recording(event)
    assert event.current_buffer.inserted == []


def test_run_transcription_reports_a_missing_model_instead_of_raising():
    buffer, app = _FakeBuffer(), _FakeApp()
    with patch.object(cli.voice, "transcribe",
                      side_effect=cli.voice.ModelNotDownloadedError("model isn't downloaded")):
        cli._run_transcription([0.0], buffer, app)  # must not raise
    assert buffer.inserted == []
    assert "isn't downloaded" in cli.UI_STATE["voice_status"]


def test_run_transcription_survives_any_other_failure():
    buffer, app = _FakeBuffer(), _FakeApp()
    with patch.object(cli.voice, "transcribe", side_effect=RuntimeError("boom")):
        cli._run_transcription([0.0], buffer, app)
    assert "boom" in cli.UI_STATE["voice_status"]
    assert app.invalidated >= 1


# ---------------------------------------------------------------------------
# /notes (B3): read-only view of the model-written notes file
# ---------------------------------------------------------------------------

def test_handle_notes_reports_when_none_saved(tmp_path):
    cli.console = Console(record=True, width=100)
    with patch.object(cli, "NOTES_PATH", tmp_path / "notes.md"):
        cli.handle_notes()
    assert "No notes saved yet" in cli.console.export_text()


def test_handle_notes_shows_saved_content(tmp_path):
    cli.console = Console(record=True, width=100)
    notes = tmp_path / "notes.md"
    notes.write_text("- the user prefers Rust\n")
    with patch.object(cli, "NOTES_PATH", notes):
        cli.handle_notes()
    assert "the user prefers Rust" in cli.console.export_text()


def test_handle_compact_reports_the_assistants_message():
    cli.console = Console(record=True, width=100)
    cli.ASSISTANT = Assistant(provider=None)
    cli.ASSISTANT.history = [{"role": "user", "content": "hi"}]
    cli.handle_compact()
    assert "Nothing to compact" in cli.console.export_text()


# ---------------------------------------------------------------------------
# /skills
# ---------------------------------------------------------------------------

def test_handle_skills_lists_every_skill_unmarked_when_none_loaded():
    cli.console = Console(record=True, width=120)
    cli.ASSISTANT = Assistant(provider=None)
    cli.handle_skills()
    out = cli.console.export_text()
    for name in tools.SKILLS:
        assert name in out
    assert "•" not in out


def test_handle_skills_marks_loaded_skills():
    cli.console = Console(record=True, width=120)
    cli.ASSISTANT = Assistant(provider=None)
    cli.ASSISTANT.active_skills.add("desktop")
    cli.handle_skills()
    out = cli.console.export_text()
    desktop_line = next(l for l in out.splitlines() if "desktop" in l)
    assert "•" in desktop_line


# ---------------------------------------------------------------------------
# /write: A3's confirmation gate on overwriting an existing file
# ---------------------------------------------------------------------------

def test_handle_write_new_file_does_not_ask(tmp_path):
    cli.console = Console(record=True, width=100)
    target = tmp_path / "new.txt"
    with patch.object(cli, "Confirm") as mock_confirm:
        cli.handle_write(f"{target} hello")
    mock_confirm.ask.assert_not_called()
    assert target.read_text() == "hello"


def test_handle_write_existing_file_asks_and_respects_denial(tmp_path):
    cli.console = Console(record=True, width=100)
    target = tmp_path / "existing.txt"
    target.write_text("old")
    with patch.object(cli, "Confirm") as mock_confirm:
        mock_confirm.ask.return_value = False
        cli.handle_write(f"{target} new")
    mock_confirm.ask.assert_called_once()
    assert target.read_text() == "old"  # untouched
    assert "Cancelled" in cli.console.export_text()


def test_handle_write_existing_file_proceeds_when_confirmed(tmp_path):
    cli.console = Console(record=True, width=100)
    target = tmp_path / "existing.txt"
    target.write_text("old")
    with patch.object(cli, "Confirm") as mock_confirm:
        mock_confirm.ask.return_value = True
        cli.handle_write(f"{target} new")
    assert target.read_text() == "new"


# ---------------------------------------------------------------------------
# is_exit_command: a real bug found and fixed (2026-09-24) -- "/exit" with
# any trailing text used to silently do nothing (routed to the inert
# handle_exit placeholder via COMMANDS), while bare "exit"/"quit" with
# trailing text got sent to the model as a real message. Verified by
# directly simulating cli.py's dispatch logic before fixing it.
# ---------------------------------------------------------------------------

def test_is_exit_command_plain_forms():
    for text in ("/exit", "/quit", "exit", "quit", "EXIT", "/Exit"):
        assert cli.is_exit_command(text) is True


def test_is_exit_command_slash_form_ignores_trailing_text():
    """The actual bug: "/exit now" used to do nothing at all."""
    assert cli.is_exit_command("/exit now") is True
    assert cli.is_exit_command("/quit later") is True


def test_is_exit_command_bare_word_requires_exact_match():
    """Deliberately NOT extended the same way as the slash form: a real
    sentence can start with the word "exit" and must reach the model."""
    assert cli.is_exit_command("exit now") is False
    assert cli.is_exit_command("exit code 1 means what?") is False


def test_is_exit_command_false_for_ordinary_input():
    assert cli.is_exit_command("hello") is False
    assert cli.is_exit_command("/help") is False
    assert cli.is_exit_command("") is False


# ---------------------------------------------------------------------------
# -p / --prompt headless mode (B4)
# ---------------------------------------------------------------------------

def test_parse_args_defaults_to_interactive():
    args = cli.parse_args([])
    assert args.prompt is None
    assert args.yes is False


def test_parse_args_prompt_and_yes():
    args = cli.parse_args(["-p", "do the thing", "-y"])
    assert args.prompt == "do the thing"
    assert args.yes is True


def test_parse_args_long_form():
    args = cli.parse_args(["--prompt", "hi", "--yes"])
    assert args.prompt == "hi" and args.yes is True


class _FakeHeadlessAssistant:
    def __init__(self, reply="ok", error=None):
        self._reply = reply
        self._error = error

    def ask(self, prompt):
        if self._error:
            raise self._error
        return self._reply


def test_run_headless_prints_the_reply_and_nothing_else(capsys):
    with patch.object(cli, "build_assistant", return_value=(_FakeHeadlessAssistant("the answer"), None)):
        cli.run_headless("a question")
    assert capsys.readouterr().out.strip() == "the answer"


def test_run_headless_denies_confirmation_by_default():
    """Headless mode must never silently approve a destructive tool call
    just because there's no one watching to see the prompt."""
    with patch.object(cli, "build_assistant", return_value=(_FakeHeadlessAssistant(), None)) as mock_build:
        cli.run_headless("a question")
    confirm_fn = mock_build.call_args[0][0]
    assert confirm_fn("delete file: important.txt") is False


def test_run_headless_yes_flag_approves_confirmations():
    with patch.object(cli, "build_assistant", return_value=(_FakeHeadlessAssistant(), None)) as mock_build:
        cli.run_headless("a question", auto_confirm=True)
    confirm_fn = mock_build.call_args[0][0]
    assert confirm_fn("delete file: important.txt") is True


def test_run_headless_exits_nonzero_and_reports_errors(capsys):
    fake = _FakeHeadlessAssistant(error=RuntimeError("boom"))
    with patch.object(cli, "build_assistant", return_value=(fake, None)):
        with pytest.raises(SystemExit) as exc_info:
            cli.run_headless("a question")
    assert exc_info.value.code == 1
    assert "I ran into an error: boom" in capsys.readouterr().out


def test_build_assistant_wires_confirm_run_and_sets_the_assistant_global():
    """Mocks every real dependency (API key, provider, store) — build_assistant's
    real Store() would otherwise touch the actual ~/.raven/history.db."""
    fake_confirm = lambda action: True
    with patch.object(cli, "get_api_key", return_value="fake-key"), \
         patch.object(cli, "OpenRouterProvider") as MockProvider, \
         patch.object(cli, "Store") as MockStore:
        MockStore.return_value.load.return_value = []
        assistant, provider = cli.build_assistant(fake_confirm)
    assert assistant.confirm_run is fake_confirm
    assert cli.ASSISTANT is assistant
    assert provider is MockProvider.return_value
