"""raven/tools.py — file/shell primitives, app matching, and the tool registry.

App-matching tests use the `app_dirs` fixture (a fabricated .desktop
directory), never the real machine's installed apps — otherwise this suite
would only pass on whichever machine wrote it.
"""
from unittest.mock import patch

import pytest

from raven import tools
from raven.platforms import linux


# ---------------------------------------------------------------------------
# Tool registry: every function has a spec and vice versa, every spec has a
# working callable — the class of bug that would silently break one tool.
# ---------------------------------------------------------------------------

def test_every_tool_function_has_a_spec():
    spec_names = {t["function"]["name"] for t in tools.TOOL_SPECS}
    assert set(tools.TOOL_FUNCTIONS) == spec_names


def test_tool_specs_are_valid_shapes():
    for spec in tools.TOOL_SPECS:
        fn = spec["function"]
        assert fn["name"] and fn["description"]
        assert fn["parameters"]["type"] == "object"


# ---------------------------------------------------------------------------
# App-name matching (Step 3.19's word-boundary fix + the "R" false-positive
# regression it introduced, both re-verified here)
# ---------------------------------------------------------------------------

def test_exact_and_generic_phrasing_both_resolve(app_dirs):
    assert tools._match_app("text editor") == ("text editor", "org.gnome.TextEditor")
    assert tools._match_app("the text editor app") == ("text editor", "org.gnome.TextEditor")
    assert tools._match_app("editor") == ("text editor", "org.gnome.TextEditor")


def test_trailing_punctuation_does_not_break_matching(app_dirs):
    assert tools._match_app("text editor.") == ("text editor", "org.gnome.TextEditor")


def test_one_letter_app_name_does_not_false_positive(app_dirs):
    """Regression: a naive substring match makes 'R' match almost any query
    (the letter appears inside 'editor'). Word-boundary matching must not."""
    result = tools._match_app("text editor")
    assert result == ("text editor", "org.gnome.TextEditor")  # not ambiguous with "R"


def test_ambiguous_name_lists_candidates_not_a_guess(app_dirs):
    result = tools._match_app("code")
    assert isinstance(result, str)
    assert "Code::Blocks Ide" in result and "Visual Studio Code" in result


def test_unknown_name_offers_closest_matches(app_dirs):
    result = tools._match_app("text editr")  # typo, close to "text editor"
    assert isinstance(result, str)
    assert "text editor" in result.lower()


def test_completely_unknown_name_has_no_crash(app_dirs):
    result = tools._match_app("zzz_nonexistent_zzz")
    assert isinstance(result, str) and "No installed app matches" in result


def test_hidden_app_is_not_matched(app_dirs):
    result = tools._match_app("hidden thing")
    assert isinstance(result, str)  # NoDisplay=true entries are excluded


def test_list_apps_excludes_hidden(app_dirs):
    listing = tools.list_apps()
    assert "Text Editor" in listing
    assert "Hidden Thing" not in listing


# ---------------------------------------------------------------------------
# _binary_name: the Flatpak fix (Step 3.21) — this is a correctness/safety
# bug, not cosmetic: close_app's pkill target came from this.
# ---------------------------------------------------------------------------

def test_binary_name_flatpak_extracts_the_real_command_not_flatpak_itself(app_dirs):
    """Before this fix, EVERY flatpak app resolved to the literal string
    'flatpak', so close_app('firefox') would have run `pkill -f flatpak` —
    matching and killing every running flatpak app, not just Firefox."""
    assert linux._binary_name("org.mozilla.firefox") == "firefox"


def test_binary_name_non_flatpak_app(app_dirs):
    assert linux._binary_name("org.gnome.TextEditor") == "gnome-text-editor"


def test_binary_name_unknown_app_id(app_dirs):
    assert linux._binary_name("no-such-app") is None


# ---------------------------------------------------------------------------
# open_file / open_url with an app=: REUSE_WINDOW_ARGS wiring (Step 3.24)
# ---------------------------------------------------------------------------

def test_open_file_injects_reuse_window_flag_for_vs_code(app_dirs, tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("x")
    with patch.object(linux, "shutil") as mock_shutil, patch.object(linux, "subprocess") as mock_subprocess:
        mock_shutil.which.return_value = "/usr/bin/code"
        result = tools.open_file(str(target), app="visual studio code")
    args = mock_subprocess.Popen.call_args[0][0]
    assert args == ["code", "-r", str(target)]
    assert "Opened" in result and "Visual Studio Code" in result


def test_open_file_other_apps_go_through_gtk_launch_unmodified(app_dirs, tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("x")
    with patch.object(linux, "shutil") as mock_shutil, patch.object(linux, "subprocess") as mock_subprocess:
        mock_shutil.which.return_value = "/usr/bin/gtk-launch"
        tools.open_file(str(target), app="code::blocks")
    args = mock_subprocess.Popen.call_args[0][0]
    assert args == ["gtk-launch", "codeblocks", str(target)]


def test_open_file_missing_target(app_dirs, tmp_path):
    result = tools.open_file(str(tmp_path / "nope.txt"))
    assert "No such file" in result


# ---------------------------------------------------------------------------
# Shared helpers used by open_url/fetch_url/web_search
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("example.com", "https://example.com"),
    ("http://example.com", "http://example.com"),
    ("https://example.com", "https://example.com"),
])
def test_normalize_url(raw, expected):
    assert tools._normalize_url(raw) == expected


def test_strip_html_removes_tags_scripts_and_collapses_whitespace():
    raw = "<html><script>evil()</script><body>  Hello   <b>world</b>  </body></html>"
    assert tools._strip_html(raw) == "Hello world"


def test_strip_html_truncates():
    raw = "<p>" + ("x" * 5000) + "</p>"
    assert len(tools._strip_html(raw, max_chars=100)) == 100


def test_unwrap_ddg_link_extracts_the_real_url():
    wrapped = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&rut=abc"
    assert tools._unwrap_ddg_link(wrapped) == "https://example.com/page"


def test_unwrap_ddg_link_passthrough_when_not_wrapped():
    assert tools._unwrap_ddg_link("https://example.com") == "https://example.com"


# ---------------------------------------------------------------------------
# File primitives — the boring but load-bearing ones
# ---------------------------------------------------------------------------

def test_write_then_read_round_trip(tmp_path):
    p = tmp_path / "f.txt"
    tools.write_file(str(p), "hello")
    assert tools.read_file(str(p)) == "hello"


def test_make_dir_is_idempotent(tmp_path):
    p = tmp_path / "a" / "b"
    tools.make_dir(str(p))
    tools.make_dir(str(p))  # must not raise on the second call
    assert p.is_dir()


# ---------------------------------------------------------------------------
# save_note (B3): durable cross-session memory, bounded on the write path.
# ---------------------------------------------------------------------------

def test_save_note_writes_a_bullet_line(tmp_path, monkeypatch):
    notes_path = tmp_path / "memory" / "notes.md"
    monkeypatch.setattr(tools, "NOTES_PATH", notes_path)
    result = tools.save_note("the user prefers Rust")
    assert "Saved note" in result
    assert notes_path.read_text() == "- the user prefers Rust\n"


def test_save_note_creates_missing_parent_directories(tmp_path, monkeypatch):
    notes_path = tmp_path / "does" / "not" / "exist" / "notes.md"
    monkeypatch.setattr(tools, "NOTES_PATH", notes_path)
    tools.save_note("x")
    assert notes_path.is_file()


def test_save_note_appends_rather_than_overwrites(tmp_path, monkeypatch):
    notes_path = tmp_path / "notes.md"
    monkeypatch.setattr(tools, "NOTES_PATH", notes_path)
    tools.save_note("first fact")
    tools.save_note("second fact")
    content = notes_path.read_text()
    assert "- first fact\n" in content
    assert "- second fact\n" in content


def test_save_note_rejects_empty_note(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "NOTES_PATH", tmp_path / "notes.md")
    assert "Error" in tools.save_note("   ")


def test_save_note_collapses_embedded_newlines_to_one_line(tmp_path, monkeypatch):
    notes_path = tmp_path / "notes.md"
    monkeypatch.setattr(tools, "NOTES_PATH", notes_path)
    tools.save_note("line one\nline two\r\nline three")
    assert notes_path.read_text().count("\n") == 1  # one note, one line


def test_save_note_rejects_an_overlong_note_rather_than_truncating(tmp_path, monkeypatch):
    notes_path = tmp_path / "notes.md"
    monkeypatch.setattr(tools, "NOTES_PATH", notes_path)
    result = tools.save_note("x" * (tools.MAX_NOTE_CHARS + 1))
    assert "too long" in result
    assert not notes_path.exists()  # refused outright, nothing partially written


def test_save_note_refuses_once_the_file_cap_is_reached(tmp_path, monkeypatch):
    notes_path = tmp_path / "notes.md"
    monkeypatch.setattr(tools, "NOTES_PATH", notes_path)
    notes_path.write_text("x" * (tools.MAX_NOTES_FILE_CHARS - 10))
    result = tools.save_note("one more fact that won't fit")
    assert "memory is full" in result
    assert notes_path.read_text() == "x" * (tools.MAX_NOTES_FILE_CHARS - 10)  # untouched, not evicted


# ---------------------------------------------------------------------------
# edit_file (C1): exact-string replace, must match uniquely
# ---------------------------------------------------------------------------

def test_edit_file_replaces_a_unique_match(tmp_path):
    p = tmp_path / "f.py"
    p.write_text("def foo():\n    return 1\n")
    result = tools.edit_file(str(p), "return 1", "return 2")
    assert "Edited" in result
    assert p.read_text() == "def foo():\n    return 2\n"


def test_edit_file_refuses_when_old_string_not_found(tmp_path):
    p = tmp_path / "f.py"
    p.write_text("original content")
    result = tools.edit_file(str(p), "nonexistent", "x")
    assert "not found" in result
    assert p.read_text() == "original content"  # untouched


def test_edit_file_refuses_an_ambiguous_match_rather_than_guessing(tmp_path):
    p = tmp_path / "f.py"
    p.write_text("x = 1\nx = 1\n")
    result = tools.edit_file(str(p), "x = 1", "x = 2")
    assert "matches 2 times" in result
    assert p.read_text() == "x = 1\nx = 1\n"  # untouched -- no guessing which one


def test_edit_file_rejects_empty_old_string(tmp_path):
    p = tmp_path / "f.py"
    p.write_text("content")
    result = tools.edit_file(str(p), "", "x")
    assert "Error" in result
    assert p.read_text() == "content"


def test_edit_file_only_replaces_the_one_match_not_every_occurrence_elsewhere(tmp_path):
    """Regression guard: str.replace(..., 1) must be used, not a global
    replace -- even though uniqueness is already enforced above this, a
    global replace on a since-duplicated string elsewhere in a bigger file
    would silently touch text it was never asked to."""
    p = tmp_path / "f.py"
    p.write_text("UNIQUE_MARKER\nsome other line\n")
    tools.edit_file(str(p), "UNIQUE_MARKER", "REPLACED")
    assert p.read_text() == "REPLACED\nsome other line\n"


# ---------------------------------------------------------------------------
# glob_files (C1)
# ---------------------------------------------------------------------------

def test_glob_files_finds_matching_files(tmp_path):
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.txt").write_text("")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.py").write_text("")
    result = tools.glob_files("**/*.py", str(tmp_path))
    assert "a.py" in result
    assert "c.py" in result
    assert "b.txt" not in result


def test_glob_files_no_matches(tmp_path):
    assert "No files matching" in tools.glob_files("*.nonexistent", str(tmp_path))


def test_glob_files_skips_heavy_directories(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("")
    (tmp_path / "real.py").write_text("")
    result = tools.glob_files("**/*", str(tmp_path))
    assert "real.py" in result
    assert ".git" not in result


def test_glob_files_not_a_directory(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    assert "Not a directory" in tools.glob_files("*", str(f))


# ---------------------------------------------------------------------------
# tree (C1)
# ---------------------------------------------------------------------------

def test_tree_shows_files_and_directories(tmp_path):
    (tmp_path / "a.py").write_text("")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("")
    result = tools.tree(str(tmp_path))
    assert "- a.py" in result
    assert "d sub" in result
    assert "- b.py" in result


def test_tree_respects_max_depth(tmp_path):
    deep = tmp_path / "l1" / "l2" / "l3"
    deep.mkdir(parents=True)
    (deep / "buried.py").write_text("")
    result = tools.tree(str(tmp_path), max_depth=1)
    assert "l1" in result
    assert "buried.py" not in result  # beyond max_depth


def test_tree_skips_heavy_directories(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_text("")
    (tmp_path / "real.py").write_text("")
    result = tools.tree(str(tmp_path))
    assert "real.py" in result
    assert "node_modules" not in result


def test_tree_not_a_directory(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    assert "Not a directory" in tools.tree(str(f))


def test_list_dir_marks_files_and_directories(tmp_path):
    (tmp_path / "file.txt").write_text("x")
    (tmp_path / "sub").mkdir()
    listing = tools.list_dir(str(tmp_path))
    assert "- file.txt" in listing
    assert "d sub" in listing


def test_list_dir_empty(tmp_path):
    assert tools.list_dir(str(tmp_path)) == "(empty)"


# ---------------------------------------------------------------------------
# _is_existing_file (A3's write_file confirmation decides on this)
# ---------------------------------------------------------------------------

def test_is_existing_file_true_for_a_real_file(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("x")
    assert tools._is_existing_file(str(p)) is True


def test_is_existing_file_false_for_a_new_path(tmp_path):
    assert tools._is_existing_file(str(tmp_path / "new.txt")) is False


def test_is_existing_file_false_for_a_directory(tmp_path):
    assert tools._is_existing_file(str(tmp_path)) is False


def test_is_existing_file_false_for_empty_path():
    assert tools._is_existing_file("") is False


# ---------------------------------------------------------------------------
# grep (A3): read-only text search, no shell involved
# ---------------------------------------------------------------------------

@pytest.fixture
def search_tree(tmp_path):
    (tmp_path / "a.py").write_text("def hello():\n    return 'world'\n")
    (tmp_path / "b.py").write_text("class Hello:\n    pass\n")
    (tmp_path / "notes.txt").write_text("nothing relevant here\n")
    (tmp_path / "data.bin").write_bytes(b"\x00\x01hello\x02\x03")
    skip_dir = tmp_path / "venv" / "lib"
    skip_dir.mkdir(parents=True)
    (skip_dir / "c.py").write_text("def hello(): pass\n")
    return tmp_path


def test_grep_finds_matches_with_file_and_line_number(search_tree):
    result = tools.grep("hello", str(search_tree))
    assert "a.py:1: def hello():" in result
    assert "b.py:1: class Hello:" in result  # case-insensitive


def test_grep_is_case_insensitive_by_design(search_tree):
    assert "b.py" in tools.grep("HELLO", str(search_tree))


def test_grep_skips_common_heavy_directories(search_tree):
    result = tools.grep("hello", str(search_tree))
    assert "venv" not in result


def test_grep_skips_binary_files(search_tree):
    result = tools.grep("hello", str(search_tree))
    assert "data.bin" not in result


def test_grep_narrowed_by_glob(search_tree):
    result = tools.grep("hello", str(search_tree), glob="**/notes.txt")
    assert "No matches" in result


def test_grep_no_matches_is_a_clear_message_not_empty_string(search_tree):
    result = tools.grep("zzz_nomatch_zzz", str(search_tree))
    assert "No matches" in result


def test_grep_invalid_regex_does_not_crash(search_tree):
    result = tools.grep("(unclosed[", str(search_tree))
    assert "Invalid pattern" in result


def test_grep_target_not_a_directory(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x")
    assert "Not a directory" in tools.grep("x", str(f))


def test_grep_caps_the_number_of_matches(tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("hit\n" * (tools.MAX_GREP_MATCHES + 20))
    result = tools.grep("hit", str(tmp_path))
    match_lines = [line for line in result.splitlines() if line.startswith("big.txt:")]
    assert len(match_lines) == tools.MAX_GREP_MATCHES
    assert "narrow the pattern" in result


def test_looks_binary_true_for_nul_byte(tmp_path):
    p = tmp_path / "b.bin"
    p.write_bytes(b"abc\x00def")
    assert tools._looks_binary(p) is True


def test_looks_binary_false_for_text(tmp_path):
    p = tmp_path / "t.txt"
    p.write_text("just text")
    assert tools._looks_binary(p) is False


def test_delete_file(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("x")
    tools.delete_file(str(p))
    assert not p.exists()


def test_run_command_captures_stdout():
    assert tools.run_command("echo hello").strip() == "hello"


def test_run_command_no_output_reports_exit_code():
    assert tools.run_command("true") == "(exited 0, no output)"


# ---------------------------------------------------------------------------
# run_tests (C3): same execution shape as run_command, longer timeout
# ---------------------------------------------------------------------------

def test_run_tests_captures_stdout():
    assert tools.run_tests("echo hello").strip() == "hello"


def test_run_tests_defaults_to_pytest(monkeypatch):
    captured = {}

    def fake_run(command, shell, capture_output, text, timeout, stdin):
        captured["command"] = command
        captured["timeout"] = timeout
        captured["stdin"] = stdin
        class R: stdout, stderr, returncode = "", "", 0
        return R()

    monkeypatch.setattr(tools.subprocess, "run", fake_run)
    tools.run_tests()
    assert captured["command"] == "pytest"
    assert captured["timeout"] == tools.RUN_TESTS_TIMEOUT
    assert tools.RUN_TESTS_TIMEOUT > tools.RUN_COMMAND_TIMEOUT  # the whole point of a separate tool
    assert captured["stdin"] == tools.subprocess.DEVNULL


# ---------------------------------------------------------------------------
# _run_shell stdin=DEVNULL (real bug, found live 2026-09-24): without it, a
# command that reads stdin (python3, ssh, sudo, git commit with no -m, ...)
# inherits R.A.V.E.N's own stdin -- the user's real terminal in interactive
# mode -- and blocks forever, in a way even Ctrl+C couldn't recover from
# (prompt_toolkit already owns the terminal in its own raw input mode).
# The actual hang-and-fix was reproduced live with a real pty before this
# fix was written (see DEV_LOG) -- that reproduction isn't repeated here as
# a routine test, since it requires reassigning the TEST PROCESS's own stdin
# fd to prove anything, which is too invasive/risky to do routinely inside
# the main suite (real risk of corrupting pytest's own I/O if anything goes
# wrong). What's safe and still meaningful to assert on every run: the
# actual subprocess.run call unconditionally includes stdin=DEVNULL, for
# every one of _run_shell's callers, not just one.
# ---------------------------------------------------------------------------

def test_run_command_and_run_tests_and_git_all_pass_stdin_devnull(monkeypatch):
    calls = []

    def fake_run(command, shell, capture_output, text, timeout, stdin):
        calls.append(stdin)
        class R: stdout, stderr, returncode = "", "", 0
        return R()

    monkeypatch.setattr(tools.subprocess, "run", fake_run)
    tools.run_command("echo hi")
    tools.run_tests()
    tools.git("status")
    assert calls == [tools.subprocess.DEVNULL] * 3


# ---------------------------------------------------------------------------
# SKILLS / core_tool_names (D2)
# ---------------------------------------------------------------------------

def test_every_skill_tool_is_a_real_registered_tool():
    """A typo'd tool name in SKILLS would silently vanish from BOTH core and
    every skill -- unreachable by the model no matter what it calls."""
    all_skill_tools = {name for names in tools.SKILLS.values() for name in names}
    assert all_skill_tools <= set(tools.TOOL_FUNCTIONS)


def test_every_skill_has_a_description():
    assert set(tools.SKILLS) == set(tools.SKILL_DESCRIPTIONS)


def test_core_tool_names_excludes_skill_tools():
    core = tools.core_tool_names()
    all_skill_tools = {name for names in tools.SKILLS.values() for name in names}
    assert core.isdisjoint(all_skill_tools)


def test_core_tool_names_includes_everything_else():
    """core + skills together must cover every registered tool -- nothing
    silently falls through the cracks and becomes permanently unreachable."""
    all_skill_tools = {name for names in tools.SKILLS.values() for name in names}
    assert tools.core_tool_names() | all_skill_tools == set(tools.TOOL_FUNCTIONS)


def test_core_includes_the_coding_essentials():
    core = tools.core_tool_names()
    for name in ("read_file", "write_file", "edit_file", "grep", "glob_files",
                 "tree", "run_command", "run_tests", "git", "save_note"):
        assert name in core


# ---------------------------------------------------------------------------
# analyze_image / browser_screenshot (V2): analyze_image is registered as a
# placeholder only -- Assistant._run_tool intercepts it before it ever runs.
# ---------------------------------------------------------------------------

def test_analyze_image_placeholder_never_actually_runs():
    """If this DOES run, the interception in Assistant._run_tool was
    skipped somehow -- it should fail loudly and clearly, not silently."""
    with pytest.raises(RuntimeError, match="must be handled by Assistant"):
        tools.analyze_image("x.png", "what is this?")


def test_vision_skill_contains_analyze_image():
    assert "analyze_image" in tools.SKILLS["vision"]


def test_browser_skill_contains_screenshot():
    assert "browser_screenshot" in tools.SKILLS["browser"]
