"""raven/cli.py — the pieces that don't need a real terminal.

Excludes main()/PromptSession/the input loop, which need a real TTY. Every
function here is pure or takes its dependencies (status, console) as
parameters/mocks.
"""
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
# confirm_run: pauses/resumes the status spinner around the blocking prompt
# (Step 3.15 — this used to make the prompt invisible)
# ---------------------------------------------------------------------------

class FakeStatus:
    def __init__(self):
        self.calls = []

    def stop(self):
        self.calls.append("stop")

    def start(self):
        self.calls.append("start")


def test_confirm_run_pauses_and_resumes_the_status_spinner():
    status = FakeStatus()
    with patch.object(cli, "Confirm") as mock_confirm:
        mock_confirm.ask.return_value = True
        result = cli.confirm_run("close: calculator", status)
    assert result is True
    assert status.calls == ["stop", "start"]


def test_confirm_run_resumes_the_spinner_even_if_the_prompt_is_interrupted():
    status = FakeStatus()
    with patch.object(cli, "Confirm") as mock_confirm:
        mock_confirm.ask.side_effect = KeyboardInterrupt
        try:
            cli.confirm_run("close: calculator", status)
        except KeyboardInterrupt:
            pass
    assert status.calls == ["stop", "start"]


def test_confirm_run_with_no_status_still_works():
    with patch.object(cli, "Confirm") as mock_confirm:
        mock_confirm.ask.return_value = False
        assert cli.confirm_run("x", None) is False


# ---------------------------------------------------------------------------
# /forget
# ---------------------------------------------------------------------------

def test_handle_forget_clears_history_and_reports_the_count():
    cli.console = Console(record=True, width=100)
    cli.ASSISTANT = Assistant(provider=None)
    cli.ASSISTANT.history = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    cli.handle_forget()
    assert "Forgot 2 messages" in cli.console.export_text()
    assert cli.ASSISTANT.history == []


# ---------------------------------------------------------------------------
# /compact (B2)
# ---------------------------------------------------------------------------

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
