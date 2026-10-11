"""raven/assistant.py — the tool loop and the confirmation policy.

The confirmation policy is the most safety-critical part of this project
(DEV_LOG Steps 3.14/3.17/3.19/3.23/3.27/3.28 each fixed a real bug here), so
it gets the most exhaustive coverage: every tool the model can call, plus the
specific injection/edge cases already found once in this project.
"""
import json
from unittest.mock import patch

import pytest

import raven.assistant as assistant_module
from raven.assistant import Assistant, strip_emoji, _coerce_numeric_strings, _ensure_visible
from raven import gmail as gmail_impl
from raven import platforms
from raven import tools as tool_impl


# ---------------------------------------------------------------------------
# Confirmation matrix: every tool the model can call, in one place, so a new
# tool that forgets to declare its confirmation status is caught immediately.
# ---------------------------------------------------------------------------

NO_CONFIRM_PROBES = {
    "read_file": {"path": "x"},
    "make_dir": {"path": "x"},
    "save_note": {"note": "x"},
    "list_dir": {},
    "grep": {"pattern": "x"},
    "glob_files": {"pattern": "x"},
    "tree": {},
    "analyze_image": {"path": "x", "question": "what is this?"},
    "browser_screenshot": {},
    "open_app": {"name": "x"},
    "open_file": {"path": "x"},
    "list_apps": {},
    "list_windows": {},
    "open_url": {"url": "x"},
    "fetch_url": {"url": "x"},
    "web_search": {"query": "x"},
    "browser_navigate": {"url": "x"},
    "browser_read": {},
    "browser_click": {"text": "x"},
    "browser_type": {"text": "x", "value": "y"},
    "browser_close": {},
    "gmail_list_messages": {},
    "gmail_read_message": {"message_id": "x"},
}

ALWAYS_CONFIRM_PROBES = {
    "edit_file": {"path": "x", "old_string": "a", "new_string": "b"},
    "delete_file": {"path": "x"},
    "close_app": {"name": "x"},
    "close_window": {"title": "x"},
    "browser_submit": {"text": "x"},
    "gmail_send_message": {"to": "a@b.com", "subject": "s", "body": "b"},
}


@pytest.mark.parametrize("name,args", NO_CONFIRM_PROBES.items(), ids=NO_CONFIRM_PROBES.keys())
def test_tools_that_never_confirm(name, args):
    assert Assistant._confirm_prompt(name, args) is None


@pytest.mark.parametrize("name,args", ALWAYS_CONFIRM_PROBES.items(), ids=ALWAYS_CONFIRM_PROBES.keys())
def test_tools_that_always_confirm(name, args):
    assert Assistant._confirm_prompt(name, args) is not None


def test_gmail_reply_always_confirms_and_shows_resolved_recipient():
    """gmail_reply_message's prompt is built by calling gmail.reply_preview()
    (a real lookup), not just echoing the arguments — verified separately in
    test_gmail.py that the resolved (not visible-sender) address is shown."""
    with patch.object(gmail_impl, "reply_preview", return_value="To: real@x.com\n  Subject: Re: hi"):
        prompt = Assistant._confirm_prompt("gmail_reply_message", {"message_id": "m", "body": "hello"})
    assert prompt is not None
    assert "real@x.com" in prompt and "hello" in prompt


# ---------------------------------------------------------------------------
# write_file: conditional confirmation (A3) — only overwriting an EXISTING
# file is consequential; creating a new one isn't. Uses real tmp_path files
# since the decision is made by a real filesystem check (tools._is_existing_file).
# ---------------------------------------------------------------------------

def test_write_file_new_file_never_confirms(tmp_path):
    target = tmp_path / "new.txt"
    assert not target.exists()
    assert Assistant._confirm_prompt("write_file", {"path": str(target), "content": "hi"}) is None


def test_write_file_existing_file_always_confirms(tmp_path):
    target = tmp_path / "existing.txt"
    target.write_text("old content")
    prompt = Assistant._confirm_prompt("write_file", {"path": str(target), "content": "new content"})
    assert prompt is not None
    assert str(target) in prompt
    assert "new content" in prompt


def test_write_file_confirmation_truncates_a_long_new_content(tmp_path):
    target = tmp_path / "existing.txt"
    target.write_text("old")
    prompt = Assistant._confirm_prompt("write_file", {"path": str(target), "content": "x" * 500})
    assert "…" in prompt
    assert len(prompt) < 500


def test_write_file_targeting_a_directory_does_not_confirm():
    """A directory isn't a file to "overwrite" — write_file will fail on it
    naturally at execution (IsADirectoryError), which is a separate concern
    from the confirmation policy."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        assert Assistant._confirm_prompt("write_file", {"path": d, "content": "x"}) is None


def test_write_file_empty_path_does_not_crash_the_confirmation_check():
    assert Assistant._confirm_prompt("write_file", {"path": "", "content": "x"}) is None


# ---------------------------------------------------------------------------
# edit_file: ALWAYS confirms (C1) — unlike write_file it only ever touches
# an existing file, so there's no "new file" exemption to check for.
# ---------------------------------------------------------------------------

def test_edit_file_confirmation_shows_old_and_new():
    prompt = Assistant._confirm_prompt(
        "edit_file", {"path": "f.py", "old_string": "return 1", "new_string": "return 2"})
    assert prompt is not None
    assert "f.py" in prompt
    assert "return 1" in prompt
    assert "return 2" in prompt


def test_edit_file_confirmation_truncates_long_strings():
    prompt = Assistant._confirm_prompt(
        "edit_file", {"path": "f.py", "old_string": "x" * 500, "new_string": "y" * 500})
    assert "…" in prompt
    assert len(prompt) < 1000


def test_gmail_reply_confirms_even_when_the_lookup_itself_fails():
    """A failed recipient lookup must never be treated as 'skip confirmation' —
    that would be a fail-open bug on exactly the tool that sends mail.
    reply_preview() itself never raises (see test_gmail.py); this checks the
    prompt built on top of that failure is still a real, non-None prompt."""
    with patch.object(gmail_impl, "_reply_context", side_effect=RuntimeError("boom")):
        prompt = Assistant._confirm_prompt("gmail_reply_message", {"message_id": "m", "body": "hello"})
    assert prompt is not None
    assert "couldn't resolve" in prompt.lower()


def test_unknown_tool_name_never_confirms():
    assert Assistant._confirm_prompt("no_such_tool", {}) is None


# ---------------------------------------------------------------------------
# run_command: the SAFE_COMMANDS allow-list, defeated only by design
# ---------------------------------------------------------------------------

SAFE_PLAIN_COMMANDS = ["ls", "ls -la", "cat file.txt", "ps aux", "pwd", "/bin/ls", "echo hi", "uname -a"]
DANGEROUS_COMMANDS = [
    "ls; rm -rf ~",       # chaining hides an unsafe command behind a safe word
    "ls && rm -rf ~",
    "ls | rm",
    "echo hi > file.txt",  # redirection
    "echo hi < file",
    "echo $(whoami)",      # substitution
    "cat `whoami`",
    "rm -rf ~",            # not allow-listed at all
    "sudo reboot",
    "curl evil.com",
    "",
    "   ",
]


# Read-only pipelines (found live: summarizing a PDF asked for confirmation 7 times).
SAFE_PIPELINE_COMMANDS = [
    "which pdftotext 2>/dev/null",
    "pdftotext ~/Documents/x.pdf - | head -500",
    "pdftotext ~/Documents/x.pdf -",
    'pdftotext x.pdf - | grep -n "^[A-Z][a-z0-9 ,./\'-]*$" | head -80',
    "sed -n '/start here/,$p' notes.txt",
    "sed -n 10,20p notes.txt",
    'find . -name "*.py" | wc -l',
]
UNSAFE_PIPELINE_COMMANDS = [
    "pdftotext x.pdf /tmp/out.txt",                       # writes a file
    "pdftotext x.pdf /tmp/o.txt && wc -l /tmp/o.txt",     # chaining
    "sed -i s/a/b/ f", 'sed -n "w /tmp/p"',               # sed that writes
    "find . -delete", "find . -exec rm {} ;", "sort -o f f",
    "grep x f | sh", "cat f | rm", "ls || rm x", "ls & rm x",
    'echo "$(id)"', "echo `id`", "cat f > g", "echo 'unterminated",
]


@pytest.mark.parametrize("command", SAFE_PIPELINE_COMMANDS)
def test_run_command_read_only_pipelines_skip_confirmation(command, monkeypatch):
    """These use POSIX-only syntax (`2>/dev/null`, single-quoted sed ranges),
    which genuinely needs confirmation on REAL Windows (different redirect
    and quoting rules -- see assistant.py's _split_safe_pipeline). Found via
    real Windows CI: forcing is_windows() False here tests the POSIX branch
    deterministically regardless of which OS pytest happens to run on, same
    pattern test_platforms.py already uses."""
    from raven import platforms
    monkeypatch.setattr(platforms, "is_windows", lambda: False)
    assert Assistant._confirm_prompt("run_command", {"command": command}) is None


@pytest.mark.parametrize("command", UNSAFE_PIPELINE_COMMANDS)
def test_run_command_pipelines_with_anything_unsafe_still_confirm(command, monkeypatch):
    from raven import platforms
    monkeypatch.setattr(platforms, "is_windows", lambda: False)
    assert Assistant._confirm_prompt("run_command", {"command": command}) is not None


@pytest.mark.parametrize("command", SAFE_PLAIN_COMMANDS)
def test_run_command_safe_commands_skip_confirmation(command):
    assert Assistant._confirm_prompt("run_command", {"command": command}) is None


@pytest.mark.parametrize("command", DANGEROUS_COMMANDS)
def test_run_command_dangerous_commands_always_confirm(command):
    assert Assistant._confirm_prompt("run_command", {"command": command}) is not None


def test_run_command_prompt_shows_the_full_command():
    prompt = Assistant._confirm_prompt("run_command", {"command": "rm -rf /tmp/x"})
    assert prompt == "run: rm -rf /tmp/x"


# ---------------------------------------------------------------------------
# git: SAFE_GIT plus the exact injection bug found and fixed in Step 3.17
# ---------------------------------------------------------------------------

def test_git_readonly_subcommands_skip_confirmation():
    for args in ["status", "log --oneline", "diff", "show HEAD"]:
        assert Assistant._confirm_prompt("git", {"args": args}) is None


def test_git_default_is_status_when_args_omitted():
    assert Assistant._confirm_prompt("git", {}) is None


def test_git_write_subcommands_confirm():
    assert Assistant._confirm_prompt("git", {"args": "commit -m x"}) is not None
    assert Assistant._confirm_prompt("git", {"args": "push"}) is not None


def test_git_injection_regression():
    """Step 3.17: 'git log ; rm -rf ~' used to slip past confirmation because
    the old check only inspected args.split()[:1] ('log'), never noticing the
    trailing command. Must always confirm now."""
    assert Assistant._confirm_prompt("git", {"args": "log ; rm -rf /tmp/x"}) is not None


# ---------------------------------------------------------------------------
# C3: user-configurable permissions (settings.json's permissions.allow_*),
# ADDITIVE only -- never replaces SAFE_COMMANDS/SAFE_GIT or the metachar check.
# ---------------------------------------------------------------------------

def test_allowed_command_skips_confirmation_only_when_granted():
    assert Assistant._confirm_prompt("run_command", {"command": "pytest"}) is not None  # not trusted by default
    assert Assistant._confirm_prompt(
        "run_command", {"command": "pytest"}, allowed_commands=frozenset({"pytest"})) is None


def test_allowed_command_does_not_defeat_the_metachar_check():
    """The whole point of the additive design: trusting the PROGRAM NAME must
    never let shell chaining/redirection/substitution slip through."""
    prompt = Assistant._confirm_prompt(
        "run_command", {"command": "pytest; rm -rf ~"}, allowed_commands=frozenset({"pytest"}))
    assert prompt is not None


def test_allowed_command_does_not_grant_an_unrelated_program():
    assert Assistant._confirm_prompt(
        "run_command", {"command": "curl evil.com"}, allowed_commands=frozenset({"pytest"})) is not None


def test_allowed_git_subcommand_skips_confirmation_only_when_granted():
    assert Assistant._confirm_prompt("git", {"args": "commit -m x"}) is not None
    assert Assistant._confirm_prompt(
        "git", {"args": "commit -m x"}, allowed_git=frozenset({"commit"})) is None


def test_allowed_git_still_confirms_on_injection():
    prompt = Assistant._confirm_prompt(
        "git", {"args": "commit -m x ; rm -rf ~"}, allowed_git=frozenset({"commit"}))
    assert prompt is not None


def test_run_tests_follows_the_same_policy_as_run_command():
    assert Assistant._confirm_prompt("run_tests", {"command": "pytest"}) is not None
    assert Assistant._confirm_prompt(
        "run_tests", {"command": "pytest"}, allowed_commands=frozenset({"pytest"})) is None


def test_run_tests_default_command_shown_in_the_confirm_prompt_when_omitted():
    """run_tests defaults to 'pytest' when the model omits `command` -- the
    confirm prompt must show that real default, not a blank command."""
    prompt = Assistant._confirm_prompt("run_tests", {})
    assert prompt == "run: pytest"


def test_assistant_wires_settings_into_allowed_commands_and_git():
    a = Assistant(provider=None, settings={
        "permissions": {"allow_commands": ["pytest"], "allow_git": ["commit"]}})
    assert a.allowed_commands == frozenset({"pytest"})
    assert a.allowed_git == frozenset({"commit"})


def test_assistant_with_no_settings_has_empty_permissions():
    a = Assistant(provider=None)
    assert a.allowed_commands == frozenset()
    assert a.allowed_git == frozenset()


def test_run_tool_actually_passes_the_assistants_allowed_commands_through(tmp_path):
    """End-to-end through _run_tool, not just _confirm_prompt directly --
    catches a regression where the wiring in _run_tool itself is dropped."""
    seen = []
    a = Assistant(
        provider=None,
        confirm_run=lambda prompt: seen.append(prompt) or True,
        settings={"permissions": {"allow_commands": ["pytest"]}},
    )
    with patch("raven.tools.TOOL_FUNCTIONS", {**__import__("raven.tools", fromlist=["x"]).TOOL_FUNCTIONS,
                                              "run_tests": lambda command="pytest": "ok"}):
        result = a._run_tool({"function": {"name": "run_tests", "arguments": "{}"}, "id": "c"})
    assert seen == []  # trusted -- never asked
    assert result == "ok"


# ---------------------------------------------------------------------------
# strip_emoji
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Hello \U0001F600 world", "Hello  world"),
    ("No emoji here.", "No emoji here."),
    ("Great! \U0001F389\U0001F525 Keep going ⭐", "Great!  Keep going "),
    ("Flag: \U0001F1EB\U0001F1F7 done", "Flag:  done"),
    ("Math: 2 < 3 and 5 > 4", "Math: 2 < 3 and 5 > 4"),
])
def test_strip_emoji(text, expected):
    assert strip_emoji(text) == expected


# ---------------------------------------------------------------------------
# Context trimming (A2: by approximate token/char budget, not message count)
# ---------------------------------------------------------------------------

def test_context_never_starts_with_an_orphaned_tool_message():
    """Real-sized turns (tool outputs can now individually be up to
    tools.MAX_OUTPUT_CHARS=4000), enough of them to comfortably exceed the
    48,000-char default budget — verifies real trimming happens, not just
    that the boundary-safety logic is correct in isolation."""
    a = Assistant(provider=None)
    padding = "x" * 3000
    for t in range(12):  # far more than 48,000 / (~4 * 3000) chars
        a.history += [
            {"role": "user", "content": f"q{t} {padding}"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": f"c{t}"}]},
            {"role": "tool", "tool_call_id": f"c{t}", "content": padding},
            {"role": "assistant", "content": f"a{t} {padding}"},
        ]
    ctx = a._context()
    assert ctx[0]["role"] == "user"
    assert len(ctx) < len(a.history)  # confirms trimming actually happened
    assert ctx[-1] == a.history[-1]


def test_context_respects_the_exact_char_budget(monkeypatch):
    """Deterministic check of the budget arithmetic itself, with a tiny
    monkeypatched budget so the exact cutoff is predictable."""
    monkeypatch.setattr(assistant_module, "MAX_CONTEXT_CHARS", 250)
    a = Assistant(provider=None)
    a.history = [
        {"role": "user", "content": "a" * 80},
        {"role": "assistant", "content": "b" * 80},
        {"role": "user", "content": "c" * 80},
        {"role": "assistant", "content": "d" * 80},
    ]
    ctx = a._context()
    # only the most recent turn fits in a 250-char budget; the older one is dropped
    assert ctx == a.history[2:]


def test_context_falls_back_to_the_last_user_turn_if_it_alone_exceeds_the_budget():
    a = Assistant(provider=None)
    a.history = [{"role": "user", "content": "big"}] + [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "x"}]} for _ in range(50)
    ]
    ctx = a._context()
    assert ctx[0]["role"] == "user"


def test_context_empty_history():
    assert Assistant(provider=None)._context() == []


def test_context_chars_matches_what_context_actually_returns():
    a = Assistant(provider=None)
    a.history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    assert a.context_chars == sum(len(json.dumps(m)) for m in a._context())


# ---------------------------------------------------------------------------
# Compaction (B2): what falls out of the budget is summarized, not dropped --
# but self.history/Store are NEVER mutated, only what _context() builds.
# ---------------------------------------------------------------------------

class SummarizingProvider:
    """A fake provider whose reply() always returns a fixed summary text --
    stands in for the dedicated LLM call _compact() makes. Records every
    call so tests can assert it wasn't (or was) called again."""
    def __init__(self, summary="SUMMARY"):
        self.summary = summary
        self.calls = []

    def reply(self, messages, tools=None):
        self.calls.append(messages)
        return {"role": "assistant", "content": self.summary}


def _big_history(turns=12, pad_chars=3000):
    padding = "x" * pad_chars
    history = []
    for t in range(turns):
        history += [
            {"role": "user", "content": f"q{t} {padding}"},
            {"role": "assistant", "content": f"a{t} {padding}"},
        ]
    return history


def test_context_without_a_provider_still_falls_back_to_a_plain_drop():
    """provider=None (used all over this test file) must keep behaving
    exactly as it did before compaction existed -- no crash, no summary."""
    a = Assistant(provider=None)
    a.history = _big_history()
    ctx = a._context()
    assert ctx[0]["role"] == "user"
    assert not any("Summary" in str(m.get("content")) for m in ctx)


def test_context_summarizes_the_dropped_prefix_instead_of_losing_it():
    provider = SummarizingProvider("the earlier gist")
    a = Assistant(provider)
    a.history = _big_history()
    ctx = a._context()
    assert len(provider.calls) == 1  # one dedicated summarization call
    assert ctx[0]["role"] == "user"
    assert "the earlier gist" in ctx[0]["content"]
    assert ctx[1:] == a.history[a._drop_boundary():]  # retained tail is untouched
    assert a.history == _big_history()  # real history/Store input is never mutated


def test_context_reuses_the_cached_summary_when_the_boundary_has_not_moved():
    provider = SummarizingProvider()
    a = Assistant(provider)
    a.history = _big_history()
    a._context()
    a._context()  # boundary unchanged -> must not re-call the model
    assert len(provider.calls) == 1


def test_context_extends_the_summary_incrementally_as_more_history_ages_out():
    provider = SummarizingProvider()
    a = Assistant(provider)
    a.history = _big_history(turns=8)
    a._context()
    first_through = a._compacted_through
    a.history += _big_history(turns=8)  # boundary advances further
    a._context()
    assert len(provider.calls) == 2
    assert a._compacted_through > first_through
    # the second call folds the prior summary in, not just the new material
    assert "EXISTING SUMMARY" in provider.calls[1][0]["content"]


def test_compact_caps_an_overlong_summary():
    from raven.assistant import MAX_SUMMARY_CHARS
    provider = SummarizingProvider("y" * (MAX_SUMMARY_CHARS + 500))
    a = Assistant(provider)
    a.history = _big_history()
    a._context()
    assert len(a._compacted_summary) <= MAX_SUMMARY_CHARS + 60  # + the truncation marker
    assert "truncated" in a._compacted_summary


def test_context_falls_back_to_a_plain_drop_if_the_summarization_call_fails():
    class FailingProvider:
        def reply(self, messages, tools=None):
            raise RuntimeError("network down")
    a = Assistant(FailingProvider())
    a.history = _big_history()
    ctx = a._context()  # must not raise
    assert ctx[0]["role"] == "user"
    assert not any("Summary" in str(m.get("content")) for m in ctx)


def test_compact_nothing_to_do_when_history_fits():
    a = Assistant(SummarizingProvider())
    a.history = [{"role": "user", "content": "hi"}]
    assert "Nothing to compact" in a.compact()


# ---------------------------------------------------------------------------
# _context_preview() / context_chars / context_len (fixed 2026-09-23): must
# NEVER trigger a real compaction call -- that's the whole bug. Found live:
# the status line calls context_chars on every toolbar refresh, including
# the very first one before any message is sent, so an oversized history
# carried over from a prior session made simply opening R.A.V.E.N block for
# as long as a real OpenRouter call takes, with no spinner, before the
# prompt would even accept input.
# ---------------------------------------------------------------------------

def test_context_chars_never_calls_the_provider_even_when_over_budget():
    provider = SummarizingProvider()
    a = Assistant(provider)
    a.history = _big_history()  # comfortably exceeds the budget
    _ = a.context_chars
    _ = a.context_len
    assert provider.calls == []  # the actual regression check


def test_context_preview_falls_back_to_the_plain_tail_before_anything_is_compacted():
    a = Assistant(SummarizingProvider())
    a.history = _big_history()
    preview = a._context_preview()
    assert preview[0]["role"] == "user"
    assert not any("Summary" in str(m.get("content")) for m in preview)
    assert preview == a.history[a._drop_boundary():]


def test_context_preview_uses_the_summary_once_a_real_turn_has_cached_one():
    """After a REAL _context() call (e.g. from an actual ask()) has cached a
    summary, the preview should reflect it -- it just can't be the one to
    CREATE that summary in the first place."""
    provider = SummarizingProvider("the gist")
    a = Assistant(provider)
    a.history = _big_history()
    a._context()  # the real, triggering call
    assert len(provider.calls) == 1
    preview = a._context_preview()
    assert "the gist" in preview[0]["content"]
    assert len(provider.calls) == 1  # preview reused the cache, didn't call again


def test_context_preview_does_not_force_a_call_when_the_boundary_has_advanced_further():
    """If more history has aged out since the last real compaction, the
    preview must still not trigger a fresh call -- it just shows the older,
    now-stale cached summary or the plain tail, whichever _drop_boundary
    indicates, without ever blocking on the model."""
    provider = SummarizingProvider("first gist")
    a = Assistant(provider)
    a.history = _big_history(turns=8)
    a._context()
    assert len(provider.calls) == 1
    a.history += _big_history(turns=8)  # boundary advances past what's cached
    _ = a.context_chars
    _ = a.context_len
    assert len(provider.calls) == 1  # still no second call from the preview path


def test_compact_reports_success_and_is_idempotent():
    provider = SummarizingProvider("gist")
    a = Assistant(provider)
    a.history = _big_history()
    msg = a.compact()
    assert "Compacted" in msg
    assert len(provider.calls) == 1
    msg2 = a.compact()
    assert "Already compacted" in msg2
    assert len(provider.calls) == 1  # no redundant call


def test_compact_reports_no_model_available():
    a = Assistant(provider=None)
    a.history = _big_history()
    assert "no model available" in a.compact()


# ---------------------------------------------------------------------------
# ask() loop, against a scripted fake provider — no network
# ---------------------------------------------------------------------------

class ScriptedProvider:
    """Replays a fixed sequence of assistant messages, one per call to reply()."""
    def __init__(self, messages):
        self._messages = list(messages)
        self.calls = []

    def reply(self, messages, tools=None):
        self.calls.append(messages)
        return self._messages.pop(0)


def test_ask_returns_final_text_and_runs_the_tool_in_between():
    seen = []
    provider = ScriptedProvider([
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": '{"path": "x"}'}}]},
        {"role": "assistant", "content": "done"},
    ])
    a = Assistant(provider, on_tool_call=lambda n, args: seen.append((n, args)))
    with patch("raven.tools.TOOL_FUNCTIONS", {**__import__("raven.tools", fromlist=["x"]).TOOL_FUNCTIONS,
                                              "read_file": lambda path: "file contents"}):
        reply = a.ask("read x")
    assert reply == "done"
    assert seen == [("read_file", {"path": "x"})]
    assert [m["role"] for m in a.history] == ["user", "assistant", "tool", "assistant"]


def test_ask_stops_at_the_iteration_cap_instead_of_looping_forever():
    from raven.assistant import MAX_TOOL_ITERATIONS
    call = {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c", "function": {"name": "list_dir", "arguments": "{}"}}]}
    provider = ScriptedProvider([call] * MAX_TOOL_ITERATIONS)
    with patch("raven.tools.TOOL_FUNCTIONS", {**__import__("raven.tools", fromlist=["x"]).TOOL_FUNCTIONS,
                                              "list_dir": lambda: "..."}):
        reply = Assistant(provider).ask("loop forever")
    assert "tool-call limit" in reply
    assert len(provider.calls) == MAX_TOOL_ITERATIONS


def test_ask_rolls_back_history_on_keyboard_interrupt():
    class InterruptingProvider:
        def reply(self, messages, tools=None):
            raise KeyboardInterrupt
    a = Assistant(InterruptingProvider())
    with pytest.raises(KeyboardInterrupt):
        a.ask("hello")
    assert a.history == []  # the half-finished user turn was rolled back


def test_ask_persists_only_completed_turns(store):
    """A cancelled turn must not leave a dangling 'user' message in the store
    with no reply — that would corrupt the next session's loaded history."""
    class InterruptingProvider:
        def reply(self, messages, tools=None):
            raise KeyboardInterrupt
    a = Assistant(InterruptingProvider(), store=store)
    with pytest.raises(KeyboardInterrupt):
        a.ask("hello")
    assert store.load() == []

    a2 = Assistant(ScriptedProvider([{"role": "assistant", "content": "hi"}]), store=store)
    a2.ask("hello")
    assert len(store.load()) == 2


def test_ask_captures_elapsed_time_and_strips_emoji_from_reasoning():
    provider = ScriptedProvider([
        {"role": "assistant", "content": "done", "reasoning": "thinking \U0001F600 hard"},
    ])
    a = Assistant(provider)
    a.ask("hi")
    assert a.last_elapsed >= 0
    assert a.last_reasoning == ["thinking  hard"]


def test_forget_clears_memory_and_store(store):
    a = Assistant(ScriptedProvider([{"role": "assistant", "content": "hi"}]), store=store)
    a.ask("hello")
    assert a.history and store.load()
    a.forget()
    assert a.history == []
    assert store.load() == []


# ---------------------------------------------------------------------------
# _run_tool: confirmation gating, unknown tools, exceptions
# ---------------------------------------------------------------------------

def _call(name, args):
    return {"id": "c1", "function": {"name": name, "arguments": json.dumps(args)}}


def test_run_tool_cancelled_when_confirmation_denied():
    confirms = []
    a = Assistant(provider=None, confirm_run=lambda action: (confirms.append(action), False)[1])
    result = a._run_tool(_call("delete_file", {"path": "/tmp/x"}))
    assert result == "Cancelled by user."
    assert confirms == ["delete file: /tmp/x"]


# ---------------------------------------------------------------------------
# Notification system: an OS notification when R.A.V.E.N needs confirmation
# or finishes a turn, so the user finds out without watching the window.
# ---------------------------------------------------------------------------

class _RecordingBackend(platforms.Backend):
    name = "Recording"

    def __init__(self):
        self.calls = []

    def notify(self, title, body):
        self.calls.append((title, body))


@pytest.fixture
def notify_backend():
    backend = _RecordingBackend()
    platforms.set_backend(backend)
    yield backend
    platforms.set_backend(None)


def test_a_confirmation_gated_tool_fires_a_notification_before_asking(notify_backend):
    order = []
    a = Assistant(
        provider=None,
        confirm_run=lambda action: (order.append("confirmed"), True)[1],
    )
    notify_backend.notify = lambda title, body: order.append("notified") or _RecordingBackend.notify(notify_backend, title, body)
    a._run_tool(_call("delete_file", {"path": "/tmp/x"}))
    assert order == ["notified", "confirmed"]
    assert notify_backend.calls == [("R.A.V.E.N needs confirmation", "delete file: /tmp/x")]


def test_a_tool_that_never_confirms_never_notifies(notify_backend):
    a = Assistant(provider=None)
    a._run_tool(_call("read_file", {"path": "/tmp/x"}))
    assert notify_backend.calls == []


def test_ask_notifies_with_the_reply_once_the_turn_finishes(notify_backend):
    provider = type("P", (), {"reply": staticmethod(
        lambda messages, tools=None: {"role": "assistant", "content": "All done."}
    )})()
    a = Assistant(provider)
    assert a.ask("do the thing") == "All done."
    assert notify_backend.calls == [("R.A.V.E.N", "All done.")]


def test_ask_notification_body_collapses_whitespace_and_truncates():
    long_reply = ("line one\n" * 50) + "end"
    body = assistant_module._notification_body(long_reply)
    assert "\n" not in body
    assert len(body) == assistant_module.NOTIFICATION_BODY_CHARS + 1  # + the "…" marker
    assert body.endswith("…")


def test_ask_does_not_notify_on_an_empty_reply(notify_backend):
    provider = type("P", (), {"reply": staticmethod(
        lambda messages, tools=None: {"role": "assistant", "content": ""}
    )})()
    a = Assistant(provider)
    a.ask("do the thing")
    assert notify_backend.calls == []


def test_run_tool_runs_when_confirmation_granted():
    with patch("raven.tools.TOOL_FUNCTIONS", {**__import__("raven.tools", fromlist=["x"]).TOOL_FUNCTIONS,
                                              "delete_file": lambda path: f"Deleted {path}"}):
        a = Assistant(provider=None, confirm_run=lambda action: True)
        result = a._run_tool(_call("delete_file", {"path": "/tmp/x"}))
    assert result == "Deleted /tmp/x"


def test_run_tool_unknown_tool_name():
    a = Assistant(provider=None)
    assert a._run_tool(_call("not_a_real_tool", {})) == "Unknown tool: not_a_real_tool"


def test_run_tool_wraps_exceptions_instead_of_crashing():
    with patch("raven.tools.TOOL_FUNCTIONS", {**__import__("raven.tools", fromlist=["x"]).TOOL_FUNCTIONS,
                                              "read_file": lambda path: 1 / 0}):
        a = Assistant(provider=None)
        result = a._run_tool(_call("read_file", {"path": "x"}))
    assert result.startswith("Error: ")


def test_on_tool_call_fires_before_confirmation_is_asked():
    """The status line ('Closing X...') must appear before the confirmation
    prompt, not after — otherwise the user sees a frozen spinner with no
    explanation while waiting on a prompt they can't yet see the reason for."""
    order = []
    a = Assistant(
        provider=None,
        on_tool_call=lambda n, args: order.append("notified"),
        confirm_run=lambda action: order.append("confirmed") or True,
    )
    with patch("raven.tools.TOOL_FUNCTIONS", {**__import__("raven.tools", fromlist=["x"]).TOOL_FUNCTIONS,
                                              "delete_file": lambda path: "ok"}):
        a._run_tool(_call("delete_file", {"path": "x"}))
    assert order == ["notified", "confirmed"]


# ---------------------------------------------------------------------------
# Hooks (D1): user-configured pre/post automation, never confirmed.
# ---------------------------------------------------------------------------

def _tools_with(**overrides):
    real = __import__("raven.tools", fromlist=["x"]).TOOL_FUNCTIONS
    return {**real, **overrides}


def test_post_hook_runs_after_a_successful_tool_call():
    a = Assistant(provider=None, settings={"hooks": {"post": {"edit_file": "echo formatted-{path}"}}})
    with patch("raven.tools.TOOL_FUNCTIONS", _tools_with(edit_file=lambda path, old_string, new_string: "Edited x")):
        result = a._run_tool(_call("edit_file", {"path": "f.py", "old_string": "a", "new_string": "b"}))
    assert "Edited x" in result
    assert "formatted-f.py" in result


def test_pre_hook_runs_before_the_tool_call():
    a = Assistant(provider=None, settings={"hooks": {"pre": {"edit_file": "echo backed-up-{path}"}}})
    with patch("raven.tools.TOOL_FUNCTIONS", _tools_with(edit_file=lambda path, old_string, new_string: "Edited x")):
        result = a._run_tool(_call("edit_file", {"path": "f.py", "old_string": "a", "new_string": "b"}))
    assert "backed-up-f.py" in result


def test_no_hook_configured_leaves_the_result_untouched():
    a = Assistant(provider=None)
    with patch("raven.tools.TOOL_FUNCTIONS", _tools_with(read_file=lambda path: "contents")):
        result = a._run_tool(_call("read_file", {"path": "f.py"}))
    assert result == "contents"


def test_hooks_never_ask_for_confirmation():
    """Being listed in settings.json IS the user's approval -- a hook must
    never itself trigger a confirmation prompt, unlike a model-initiated
    run_command call."""
    confirms = []
    a = Assistant(
        provider=None,
        confirm_run=lambda action: confirms.append(action) or True,
        settings={"hooks": {"post": {"read_file": "echo done"}}},
    )
    with patch("raven.tools.TOOL_FUNCTIONS", _tools_with(read_file=lambda path: "contents")):
        a._run_tool(_call("read_file", {"path": "f.py"}))
    assert confirms == []


def test_post_hook_is_skipped_when_the_tool_call_itself_errored():
    """Running a formatter after edit_file's own "Error: not found" refusal
    would be pointless -- nothing actually changed."""
    a = Assistant(provider=None, settings={"hooks": {"post": {"edit_file": "echo should-not-run"}}})
    with patch("raven.tools.TOOL_FUNCTIONS", _tools_with(
            edit_file=lambda path, old_string, new_string: "Error: old_string not found in f.py.")):
        result = a._run_tool(_call("edit_file", {"path": "f.py", "old_string": "a", "new_string": "b"}))
    assert "should-not-run" not in result


def test_hook_missing_placeholder_reports_an_error_instead_of_crashing():
    a = Assistant(provider=None, settings={"hooks": {"post": {"read_file": "echo {nonexistent_arg}"}}})
    with patch("raven.tools.TOOL_FUNCTIONS", _tools_with(read_file=lambda path: "contents")):
        result = a._run_tool(_call("read_file", {"path": "f.py"}))
    assert "contents" in result
    assert "missing placeholder" in result


def test_hook_raising_an_exception_does_not_lose_the_tools_own_result():
    a = Assistant(provider=None, settings={"hooks": {"post": {"read_file": "echo x"}}})
    with patch("raven.tools.TOOL_FUNCTIONS", _tools_with(read_file=lambda path: "contents")), \
         patch.object(assistant_module.tool_impl, "run_command", side_effect=RuntimeError("boom")):
        result = a._run_tool(_call("read_file", {"path": "f.py"}))
    assert "contents" in result  # the tool's own result always survives
    assert "hook error" in result


def test_assistant_with_no_settings_has_no_hooks_configured():
    a = Assistant(provider=None)
    assert a.hooks == {"pre": {}, "post": {}}


# ---------------------------------------------------------------------------
# Skills (D2): only core tools are sent by default; load_skill unlocks the
# rest on demand, to cut the fixed per-request tool-spec overhead.
# ---------------------------------------------------------------------------

def test_active_tool_specs_defaults_to_core_plus_load_skill():
    a = Assistant(provider=None)
    names = {s["function"]["name"] for s in a._active_tool_specs()}
    assert names == tool_impl.core_tool_names() | {"load_skill"}
    assert "gmail_send_message" not in names
    assert "browser_navigate" not in names
    assert "open_app" not in names
    assert "read_file" in names  # a core tool is always present


def test_load_skill_unlocks_its_tools():
    a = Assistant(provider=None)
    result = a._load_skill("gmail")
    assert "Loaded skill 'gmail'" in result
    names = {s["function"]["name"] for s in a._active_tool_specs()}
    assert "gmail_send_message" in names
    assert "gmail_read_message" in names
    assert "open_app" not in names  # a DIFFERENT skill stays locked


def test_load_skill_unknown_name():
    a = Assistant(provider=None)
    result = a._load_skill("nonexistent")
    assert "Unknown skill" in result
    assert a.active_skills == set()


def test_load_skill_is_idempotent():
    a = Assistant(provider=None)
    a._load_skill("web")
    result = a._load_skill("web")
    assert "already loaded" in result


def test_load_skill_extends_the_providers_system_prompt():
    class FakeProvider:
        def __init__(self):
            self.system_prompt = "BASE"

    provider = FakeProvider()
    a = Assistant(provider)
    a._load_skill("browser")
    assert provider.system_prompt.startswith("BASE")
    assert "Browser skill loaded" in provider.system_prompt


def test_load_skill_with_no_system_prompt_attribute_does_not_crash():
    """A fake/test provider with no .system_prompt at all (most of this
    file's fakes) must not break load_skill -- the tools still unlock,
    which is the larger of the two benefits."""
    a = Assistant(provider=None)
    result = a._load_skill("web")  # provider is None -- no system_prompt attribute
    assert "Loaded skill 'web'" in result


def test_load_skill_with_a_real_provider_whose_system_prompt_is_still_none_does_not_crash():
    """Regression guard for a real bug: LLMProvider declares system_prompt
    with a None default (for typing reasons -- see llm_provider.py), so a
    REAL LLMProvider subclass that never sets it to an actual string has
    hasattr(provider, "system_prompt") == True but the VALUE is None --
    `self.provider.system_prompt += addition` used to crash with
    "unsupported operand type(s) for +=: 'NoneType' and 'str'" in exactly
    this case, which plain hasattr couldn't distinguish from "has a real
    string". Found live, fixed with `getattr(..., None) is not None`."""
    from raven.llm_provider import LLMProvider

    class MinimalProvider(LLMProvider):
        def reply(self, messages, tools=None):
            return {"role": "assistant", "content": "hi"}

    provider = MinimalProvider()
    assert provider.system_prompt is None  # the exact condition that crashed
    a = Assistant(provider)
    result = a._load_skill("web")  # must not raise
    assert "Loaded skill 'web'" in result
    assert provider.system_prompt is None  # still untouched -- nothing to append to


def test_run_tool_routes_load_skill_before_the_generic_dispatch():
    """End-to-end through _run_tool -- confirms load_skill never goes
    through _confirm_prompt/hooks/TOOL_FUNCTIONS, all of which don't apply
    to it and would either misbehave or error if it fell through to them."""
    confirms = []
    a = Assistant(provider=None, confirm_run=lambda p: confirms.append(p) or True)
    result = a._run_tool(_call("load_skill", {"name": "web"}))
    assert "Loaded skill 'web'" in result
    assert confirms == []  # never asked for confirmation
    assert "web" in a.active_skills


def test_provider_receives_only_active_specs():
    """Regression guard: _run_turn must call _active_tool_specs(), not the
    old tool_impl.TOOL_SPECS constant directly -- the whole point of D2."""
    captured = {}

    class SpyProvider:
        def reply(self, messages, tools=None):
            captured["tools"] = tools
            return {"role": "assistant", "content": "hi"}

    a = Assistant(SpyProvider())
    a.ask("hello")
    names = {s["function"]["name"] for s in captured["tools"]}
    assert names == tool_impl.core_tool_names() | {"load_skill"}


def test_active_skills_never_shrinks_the_core_set():
    a = Assistant(provider=None)
    a._load_skill("gmail")
    a._load_skill("desktop")
    names = {s["function"]["name"] for s in a._active_tool_specs()}
    assert tool_impl.core_tool_names() <= names


# ---------------------------------------------------------------------------
# analyze_image / _analyze_image (V2): needs self.provider, intercepted in
# _run_tool the same way load_skill is -- never confirmed, never raises.
# ---------------------------------------------------------------------------

class VisionProvider:
    """Fake provider with reply_with_image, recording every call."""
    def __init__(self, answer="a red circle"):
        self.answer = answer
        self.calls = []

    def reply_with_image(self, question, image_b64, mime_type, model):
        self.calls.append((question, image_b64, mime_type, model))
        return self.answer


def test_analyze_image_with_no_vision_model_configured():
    a = Assistant(provider=VisionProvider())  # no settings -> no vision_model
    result = a._analyze_image("x.png", "what is this?")
    assert "no vision model configured" in result.lower()


def test_analyze_image_with_a_provider_that_does_not_support_it():
    """Duck-typed fake, not inheriting from LLMProvider at all -- hasattr is
    False, no reply_with_image attribute exists at all."""
    class PlainProvider:
        def reply(self, messages, tools=None):
            return {"role": "assistant", "content": "x"}
    a = Assistant(PlainProvider(), settings={"model": {"vision": "some-vision-model"}})
    result = a._analyze_image("x.png", "what is this?")
    assert "doesn't support image analysis" in result


def test_analyze_image_with_a_real_llm_provider_that_does_not_override_it(tmp_path):
    """A REAL LLMProvider subclass that just doesn't override reply_with_image
    -- hasattr is True (inherited from the ABC), so this exercises the OTHER
    path: the base class's NotImplementedError default, caught by the
    surrounding try/except and reported as a normal Error string, not a
    crash. Uses a real, EXISTING file (unlike the duck-typed-provider test
    above) so it actually reaches the reply_with_image call, rather than
    returning early on a "file not found" it isn't testing here."""
    from raven.llm_provider import LLMProvider

    class MinimalProvider(LLMProvider):
        def reply(self, messages, tools=None):
            return {"role": "assistant", "content": "x"}

    image = tmp_path / "photo.png"
    image.write_bytes(b"fake png bytes")
    a = Assistant(MinimalProvider(), settings={"model": {"vision": "some-vision-model"}})
    result = a._analyze_image(str(image), "what is this?")
    assert result.startswith("Error:")
    assert "does not support image analysis" in result


def test_analyze_image_unreadable_file_reports_an_error_not_a_crash(tmp_path):
    provider = VisionProvider()
    a = Assistant(provider, settings={"model": {"vision": "some-vision-model"}})
    missing = tmp_path / "does_not_exist.png"
    result = a._analyze_image(str(missing), "what is this?")
    assert "Error" in result
    assert provider.calls == []  # never even attempted the API call


def test_analyze_image_reads_the_real_file_and_calls_the_vision_model(tmp_path):
    image = tmp_path / "photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake png bytes")
    provider = VisionProvider("a red circle on a white background")
    a = Assistant(provider, settings={"model": {"vision": "some-vision-model"}})
    result = a._analyze_image(str(image), "describe this")
    assert result == "a red circle on a white background"
    assert len(provider.calls) == 1
    question, image_b64, mime_type, model = provider.calls[0]
    assert question == "describe this"
    assert mime_type == "image/png"
    assert model == "some-vision-model"
    import base64
    assert base64.b64decode(image_b64) == image.read_bytes()


def test_analyze_image_resolves_the_vision_model_for_the_active_provider(tmp_path):
    """The exact bug this is a regression test for: a per-provider vision
    dict must resolve against model.provider, not be passed through
    unresolved (which would send the whole dict as the 'model' field and
    404/500 against the real API) and not silently reuse a DIFFERENT
    provider's model slug. See config.get_vision_model + DEV_LOG Step 3.79."""
    image = tmp_path / "photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake png bytes")
    provider = VisionProvider("red")
    a = Assistant(provider, settings={
        "model": {"provider": "nvidia", "vision": {"openrouter": "or-model", "nvidia": "nv-model"}},
    })
    assert a.vision_model == "nv-model"
    a._analyze_image(str(image), "what color?")
    _, _, _, model = provider.calls[0]
    assert model == "nv-model"


def test_analyze_image_propagates_a_failed_api_call_as_an_error_string():
    class FailingProvider:
        def reply_with_image(self, question, image_b64, mime_type, model):
            raise RuntimeError("OpenRouter 429: rate limited")
    a = Assistant(FailingProvider(), settings={"model": {"vision": "some-vision-model"}})
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        result = a._analyze_image(path, "what is this?")
        assert result.startswith("Error:")
    finally:
        os.remove(path)


def test_run_tool_routes_analyze_image_before_the_generic_dispatch():
    """End-to-end through _run_tool, like the equivalent load_skill test --
    confirms analyze_image never goes through _confirm_prompt/hooks/the
    real TOOL_FUNCTIONS placeholder (which would raise if actually called)."""
    confirms = []
    image = None
    provider = VisionProvider("a cat")
    a = Assistant(provider, confirm_run=lambda p: confirms.append(p) or True,
                  settings={"model": {"vision": "some-vision-model"}})
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".png")
    os.write(fd, b"fake image bytes")
    os.close(fd)
    try:
        result = a._run_tool(_call("analyze_image", {"path": path, "question": "what animal is this?"}))
    finally:
        os.remove(path)
    assert result == "a cat"
    assert confirms == []
    assert provider.calls[0][0] == "what animal is this?"


def test_analyze_image_defaults_the_question_when_omitted():
    provider = VisionProvider("a landscape")
    a = Assistant(provider, settings={"model": {"vision": "some-vision-model"}})
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".png")
    os.write(fd, b"x")
    os.close(fd)
    try:
        a._run_tool(_call("analyze_image", {"path": path}))
    finally:
        os.remove(path)
    assert provider.calls[0][0] == "Describe this image."


# ---------------------------------------------------------------------------
# Design mode: mathematically-generated shapes on the session's canvas.
# Every draw_*/list/update/delete_canvas_element name is intercepted in
# _run_tool BEFORE _confirm_prompt is ever reached (same shape as
# load_skill/analyze_image) -- so unlike NO_CONFIRM_PROBES above, the
# meaningful check here is that _run_tool never calls confirm_run for these,
# not just that _confirm_prompt(name, ...) happens to return None.
# ---------------------------------------------------------------------------

DESIGN_TOOL_NAMES = tool_impl.SKILLS["design"]


@pytest.mark.parametrize("name", DESIGN_TOOL_NAMES)
def test_design_tools_are_in_the_design_skill_and_have_specs(name):
    spec_names = {s["function"]["name"] for s in tool_impl.TOOL_SPECS}
    assert name in spec_names


def test_draw_polygon_never_asks_for_confirmation():
    confirms = []
    a = Assistant(provider=None, confirm_run=lambda p: confirms.append(p) or True)
    result = a._run_tool(_call("draw_polygon", {"sides": 6, "radius": 80}))
    assert confirms == []
    assert "Drew" in result
    assert len(a.canvas) == 1
    assert a.canvas[0]["type"] == "polygon"
    assert a.canvas[0]["generator"] == "regular_polygon"
    assert len(a.canvas[0]["points"]) == 6


def test_coerce_numeric_strings_converts_ints_and_floats_recursively():
    assert _coerce_numeric_strings({
        "sides": "6", "amount": "1.5", "fill": "none", "motif": "regular_polygon",
        "nested": {"a": "3"}, "points": [["1", "2.5"], ["3", "4"]],
    }) == {
        "sides": 6, "amount": 1.5, "fill": "none", "motif": "regular_polygon",
        "nested": {"a": 3}, "points": [[1, 2.5], [3, 4]],
    }


def test_coerce_numeric_strings_leaves_already_numeric_and_non_numeric_values_alone():
    assert _coerce_numeric_strings({"sides": 6, "fill": "#ff0000", "flag": True}) == {
        "sides": 6, "fill": "#ff0000", "flag": True,
    }


def test_draw_polygon_works_even_when_the_model_sends_numeric_args_as_strings():
    """Real bug, found live against a real NVIDIA-hosted model: it sent
    {"sides": "6", "radius": "80", "x": "0", ...} -- JSON strings, not
    numbers -- for a tool spec that declares them "integer"/"number". The
    spec is only a hint to the model, never enforced on the wire, and this
    model (not a fabricated edge case) ignored it on every single call,
    crashing draw_polygon 100% of the time with "can't multiply sequence by
    non-int of type 'float'" before this fix -- nothing ever appeared on
    the canvas, exactly matching the user's report."""
    a = Assistant(provider=None)
    result = a._run_tool(_call("draw_polygon", {
        "sides": "6", "radius": "80", "x": "0", "y": "0", "rotation": "0",
        "fill": "none", "stroke": "none", "stroke_width": "1",
    }))
    assert "Drew" in result
    assert not result.startswith("Error")
    assert len(a.canvas) == 1
    el = a.canvas[0]
    assert el["params"] == {"sides": 6, "radius": 80}
    assert el["x"] == 0 and el["y"] == 0 and el["rotation"] == 0
    assert len(el["points"]) == 6


def test_draw_radial_pattern_coerces_numeric_strings_in_nested_motif_params():
    a = Assistant(provider=None)
    result = a._run_tool(_call("draw_radial_pattern", {
        "motif": "regular_polygon", "motif_params": {"sides": "6", "radius": "20"},
        "repetitions": "6", "orbit_radius": "100",
    }))
    assert "Drew" in result
    el = a.canvas[0]
    assert el["params"]["repetitions"] == 6
    assert el["params"]["motif_params"] == {"sides": 6, "radius": 20}


def test_update_canvas_element_coerces_numeric_strings_in_the_patch():
    a = Assistant(provider=None)
    a._run_tool(_call("draw_polygon", {"sides": 6, "radius": 80}))
    element_id = a.canvas[0]["id"]
    result = a._run_tool(_call("update_canvas_element", {
        "element_id": element_id, "patch": {"x": "150", "params": {"sides": "8"}},
    }))
    assert "Updated" in result
    assert a.canvas[0]["x"] == 150
    assert a.canvas[0]["params"]["sides"] == 8
    assert len(a.canvas[0]["points"]) == 8


def test_apply_canvas_edit_coerces_numeric_strings_from_the_ui_too():
    a = Assistant(provider=None)
    a._run_tool(_call("draw_polygon", {"sides": 6, "radius": 80}))
    element_id = a.canvas[0]["id"]
    el = a.apply_canvas_edit(element_id, {"x": "42", "rotation": "90"})
    assert el["x"] == 42 and el["rotation"] == 90


def test_ensure_visible_adds_a_fallback_stroke_when_both_fill_and_stroke_are_none():
    el = {"fill": "none", "stroke": "none"}
    _ensure_visible(el)
    assert el["stroke"] != "none"
    assert el["stroke_width"]


def test_ensure_visible_leaves_a_shape_with_a_real_fill_or_stroke_alone():
    el = {"fill": "#ff0000", "stroke": "none"}
    _ensure_visible(el)
    assert el["stroke"] == "none"  # untouched -- fill alone already makes it visible

    el2 = {"fill": "none", "stroke": "#00ff00"}
    _ensure_visible(el2)
    assert el2["stroke"] == "#00ff00"


def test_draw_polygon_is_never_invisible_even_when_the_model_asks_for_no_fill_and_no_stroke():
    """Real bug, found live against a real model: it drew a circle-like
    polygon with fill="none" AND stroke="none" (its own default) -- the
    tool call succeeded and said "Drew ...", but nothing was actually
    visible, which looks exactly like the original 'nothing draws' report."""
    a = Assistant(provider=None)
    result = a._run_tool(_call("draw_polygon", {
        "sides": 100, "radius": 50, "fill": "none", "stroke": "none",
    }))
    assert "Drew" in result
    el = a.canvas[0]
    assert not (el["fill"] in ("none", "") and el["stroke"] in ("none", ""))


def test_update_canvas_element_also_guards_against_becoming_invisible():
    a = Assistant(provider=None)
    a._run_tool(_call("draw_polygon", {"sides": 6, "radius": 80, "fill": "#ff0000"}))
    element_id = a.canvas[0]["id"]
    a._run_tool(_call("update_canvas_element", {
        "element_id": element_id, "patch": {"fill": "none"},
    }))
    el = a.canvas[0]
    assert not (el["fill"] in ("none", "") and el["stroke"] in ("none", ""))


def test_update_canvas_element_does_not_touch_visibility_when_unrelated_fields_change():
    a = Assistant(provider=None)
    a._run_tool(_call("draw_polygon", {"sides": 6, "radius": 80, "fill": "none", "stroke": "none"}))
    element_id = a.canvas[0]["id"]
    # This shape is already "fixed up" to be visible by the guard above --
    # moving it shouldn't re-trigger or otherwise disturb its stroke.
    stroke_before = a.canvas[0]["stroke"]
    a._run_tool(_call("update_canvas_element", {"element_id": element_id, "patch": {"x": 10}}))
    assert a.canvas[0]["stroke"] == stroke_before


def test_draw_tools_place_the_shape_at_the_given_position():
    a = Assistant(provider=None)
    a._run_tool(_call("draw_star", {"points": 5, "outer_radius": 100, "inner_radius": 40,
                                     "x": 50, "y": -30, "fill": "#ff0000"}))
    el = a.canvas[0]
    assert el["x"] == 50 and el["y"] == -30 and el["fill"] == "#ff0000"


def test_draw_tools_assign_increasing_z_order():
    a = Assistant(provider=None)
    a._run_tool(_call("draw_polygon", {"sides": 3, "radius": 10}))
    a._run_tool(_call("draw_polygon", {"sides": 4, "radius": 10}))
    assert [el["z"] for el in a.canvas] == [0, 1]


def test_draw_tool_calls_on_canvas_update_with_the_new_element():
    seen = []
    a = Assistant(provider=None, on_canvas_update=lambda el: seen.append(el))
    a._run_tool(_call("draw_golden_rectangle", {"width": 100}))
    assert len(seen) == 1
    assert seen[0]["id"] == a.canvas[0]["id"]


def test_draw_radial_pattern_builds_a_group_from_a_named_motif():
    a = Assistant(provider=None)
    result = a._run_tool(_call("draw_radial_pattern", {
        "motif": "regular_polygon", "motif_params": {"sides": 6, "radius": 20},
        "repetitions": 6, "orbit_radius": 100,
    }))
    assert "Drew" in result
    el = a.canvas[0]
    assert el["type"] == "group" and el["generator"] == "radial_pattern"
    assert len(el["children"]) == 6


def test_draw_radial_pattern_rejects_an_unknown_motif():
    a = Assistant(provider=None)
    result = a._run_tool(_call("draw_radial_pattern", {"motif": "not_a_thing", "motif_params": {}}))
    assert result.startswith("Error:")
    assert a.canvas == []


def test_list_canvas_elements_reports_an_empty_canvas():
    a = Assistant(provider=None)
    assert a._run_tool(_call("list_canvas_elements", {})) == "The canvas is empty."


def test_list_canvas_elements_describes_what_was_drawn():
    a = Assistant(provider=None)
    a._run_tool(_call("draw_polygon", {"sides": 6, "radius": 80}))
    listing = a._run_tool(_call("list_canvas_elements", {}))
    assert a.canvas[0]["id"] in listing
    assert "regular_polygon" in listing


def test_update_canvas_element_regenerates_geometry_from_new_params():
    a = Assistant(provider=None)
    a._run_tool(_call("draw_polygon", {"sides": 6, "radius": 80}))
    element_id = a.canvas[0]["id"]
    result = a._run_tool(_call("update_canvas_element", {
        "element_id": element_id, "patch": {"params": {"sides": 8}},
    }))
    assert "Updated" in result
    assert len(a.canvas[0]["points"]) == 8
    assert a.canvas[0]["params"]["sides"] == 8


def test_update_canvas_element_can_change_position_and_color_without_params():
    a = Assistant(provider=None)
    a._run_tool(_call("draw_polygon", {"sides": 6, "radius": 80}))
    element_id = a.canvas[0]["id"]
    a._run_tool(_call("update_canvas_element", {
        "element_id": element_id, "patch": {"x": 200, "fill": "#00ff00"},
    }))
    assert a.canvas[0]["x"] == 200 and a.canvas[0]["fill"] == "#00ff00"


def test_update_canvas_element_unknown_id_is_a_clear_error():
    a = Assistant(provider=None)
    result = a._run_tool(_call("update_canvas_element", {"element_id": "nope", "patch": {"x": 1}}))
    assert result.startswith("Error:")


def test_delete_canvas_element_removes_it_and_notifies():
    deleted_ids = []
    a = Assistant(provider=None, on_canvas_delete=lambda eid: deleted_ids.append(eid))
    a._run_tool(_call("draw_polygon", {"sides": 6, "radius": 80}))
    element_id = a.canvas[0]["id"]
    result = a._run_tool(_call("delete_canvas_element", {"element_id": element_id}))
    assert "Deleted" in result
    assert a.canvas == []
    assert deleted_ids == [element_id]


def test_delete_canvas_element_unknown_id_is_a_clear_error():
    a = Assistant(provider=None)
    result = a._run_tool(_call("delete_canvas_element", {"element_id": "nope"}))
    assert result.startswith("Error:")


def test_canvas_persists_and_reloads_via_the_store(tmp_path):
    from raven.store import Store
    store = Store(tmp_path / "h.db")
    a = Assistant(provider=None, store=store)
    a._run_tool(_call("draw_polygon", {"sides": 6, "radius": 80}))
    reloaded = Assistant(provider=None, store=Store(tmp_path / "h.db", session_id=store.session_id))
    assert len(reloaded.canvas) == 1
    assert reloaded.canvas[0]["params"]["sides"] == 6


# -- user hand-edits: apply_canvas_edit / delete_canvas_element_by_user / grouping ---

def test_apply_canvas_edit_updates_the_element_and_records_a_pending_change():
    a = Assistant(provider=None)
    a._run_tool(_call("draw_polygon", {"sides": 6, "radius": 80}))
    element_id = a.canvas[0]["id"]
    el = a.apply_canvas_edit(element_id, {"x": 42})
    assert el["x"] == 42
    assert a._pending_canvas_changes  # something was recorded


def test_apply_canvas_edit_unknown_id_returns_none_and_records_nothing():
    a = Assistant(provider=None)
    assert a.apply_canvas_edit("nope", {"x": 1}) is None
    assert a._pending_canvas_changes == []


def test_canvas_delta_summary_is_injected_once_then_cleared():
    """The user's hand-edit shows up as a one-off context message on
    R.A.V.E.N's NEXT turn, then doesn't repeat on the turn after that."""
    provider = ScriptedProvider([
        {"role": "assistant", "content": "ok"},
        {"role": "assistant", "content": "ok again"},
    ])
    a = Assistant(provider)
    a._run_tool(_call("draw_polygon", {"sides": 6, "radius": 80}))
    a.apply_canvas_edit(a.canvas[0]["id"], {"x": 99})
    a.ask("what's on the canvas?")
    first_context = provider.calls[0]
    assert any("Canvas changed" in (m.get("content") or "") for m in first_context)
    a.ask("anything else?")
    second_context = provider.calls[-1]
    assert not any("Canvas changed" in (m.get("content") or "") for m in second_context)


def test_delete_canvas_element_by_user_records_a_pending_change():
    a = Assistant(provider=None)
    a._run_tool(_call("draw_polygon", {"sides": 6, "radius": 80}))
    element_id = a.canvas[0]["id"]
    assert a.delete_canvas_element_by_user(element_id) is True
    assert a.canvas == []
    assert any("deleted" in c.lower() for c in a._pending_canvas_changes)


def test_delete_canvas_element_by_user_unknown_id_returns_false():
    a = Assistant(provider=None)
    assert a.delete_canvas_element_by_user("nope") is False


def test_group_elements_positions_children_relative_to_the_centroid():
    a = Assistant(provider=None)
    a._run_tool(_call("draw_polygon", {"sides": 6, "radius": 10, "x": 0, "y": 0}))
    a._run_tool(_call("draw_star", {"points": 5, "outer_radius": 10, "inner_radius": 4, "x": 100, "y": 0}))
    ids = [el["id"] for el in a.canvas]
    group = a.group_elements(ids)
    assert group is not None and group["type"] == "group"
    assert group["x"] == 50 and group["y"] == 0
    assert len(a.canvas) == 1  # the two originals were absorbed into the group
    xs = sorted(child["x"] for child in group["children"])
    assert xs == [-50, 50]


def test_group_elements_needs_at_least_two_elements():
    a = Assistant(provider=None)
    a._run_tool(_call("draw_polygon", {"sides": 6, "radius": 10}))
    assert a.group_elements([a.canvas[0]["id"]]) is None


def test_ungroup_elements_restores_original_absolute_positions():
    a = Assistant(provider=None)
    a._run_tool(_call("draw_polygon", {"sides": 6, "radius": 10, "x": 0, "y": 0}))
    a._run_tool(_call("draw_star", {"points": 5, "outer_radius": 10, "inner_radius": 4, "x": 100, "y": 0}))
    ids = [el["id"] for el in a.canvas]
    group = a.group_elements(ids)
    restored = a.ungroup_elements(group["id"])
    assert {el["id"] for el in restored} == set(ids)
    xs = sorted(el["x"] for el in restored)
    assert xs == pytest.approx([0, 100])
    assert all(el["id"] != group["id"] for el in a.canvas)


def test_ungroup_elements_after_moving_the_group_offsets_children_correctly():
    a = Assistant(provider=None)
    a._run_tool(_call("draw_polygon", {"sides": 6, "radius": 10, "x": 0, "y": 0}))
    a._run_tool(_call("draw_star", {"points": 5, "outer_radius": 10, "inner_radius": 4, "x": 100, "y": 0}))
    ids = [el["id"] for el in a.canvas]
    group = a.group_elements(ids)
    a.apply_canvas_edit(group["id"], {"x": group["x"] + 20, "y": group["y"] + 5})
    restored = a.ungroup_elements(group["id"])
    xs = sorted(el["x"] for el in restored)
    assert xs == pytest.approx([20, 120])
    assert all(el["y"] == pytest.approx(5) for el in restored)


def test_ungroup_elements_unknown_id_returns_none():
    a = Assistant(provider=None)
    assert a.ungroup_elements("nope") is None


def test_restore_element_puts_it_back_with_the_same_id():
    seen = []
    a = Assistant(provider=None, on_canvas_update=lambda el: seen.append(el))
    a._run_tool(_call("draw_polygon", {"sides": 6, "radius": 80}))
    element_id = a.canvas[0]["id"]
    deleted = a.canvas[0]
    a.delete_canvas_element_by_user(element_id)
    assert a.canvas == []
    restored = a.restore_element(deleted)
    assert restored["id"] == element_id
    assert a.canvas == [deleted]
    assert seen[-1]["id"] == element_id
