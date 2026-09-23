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
from raven.assistant import Assistant, strip_emoji
from raven import gmail as gmail_impl
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
