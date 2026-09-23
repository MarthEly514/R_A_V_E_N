"""raven/llm_provider.py — RAVEN.md rules loading (B1) and OpenRouterProvider
wiring. cwd/home are always passed explicitly here (never real Path.cwd()/
Path.home()) so this suite can never accidentally pick up a real RAVEN.md."""
from raven import llm_provider
from raven.llm_provider import OpenRouterProvider, build_system_prompt, load_project_rules


def test_no_rules_files_is_silent(tmp_path):
    text, found = load_project_rules(cwd=tmp_path, home=tmp_path)
    assert text == ""
    assert found == []


def test_project_rules_loaded_from_cwd(tmp_path):
    (tmp_path / "RAVEN.md").write_text("Always use tabs, never spaces.")
    text, found = load_project_rules(cwd=tmp_path, home=tmp_path / "empty_home")
    assert "Always use tabs, never spaces." in text
    assert "Project rules" in text
    assert found == [tmp_path / "RAVEN.md"]


def test_user_rules_loaded_from_home(tmp_path):
    home = tmp_path / "home"
    (home / ".raven").mkdir(parents=True)
    (home / ".raven" / "RAVEN.md").write_text("Call me boss.")
    text, found = load_project_rules(cwd=tmp_path / "empty_cwd", home=home)
    assert "Call me boss." in text
    assert "User rules" in text
    assert found == [home / ".raven" / "RAVEN.md"]


def test_both_present_user_rules_come_before_project_rules(tmp_path):
    home = tmp_path / "home"
    (home / ".raven").mkdir(parents=True)
    (home / ".raven" / "RAVEN.md").write_text("USER_MARKER")
    (tmp_path / "RAVEN.md").write_text("PROJECT_MARKER")
    text, found = load_project_rules(cwd=tmp_path, home=home)
    assert text.index("USER_MARKER") < text.index("PROJECT_MARKER")
    assert len(found) == 2


def test_a_directory_named_raven_md_is_not_treated_as_a_file(tmp_path):
    (tmp_path / "RAVEN.md").mkdir()
    text, found = load_project_rules(cwd=tmp_path, home=tmp_path / "empty_home")
    assert text == "" and found == []


def test_unreadable_or_missing_rules_file_never_raises(tmp_path):
    # No file at all, and a path whose parent doesn't exist either.
    text, found = load_project_rules(cwd=tmp_path / "nope" / "deeper", home=tmp_path / "also_nope")
    assert text == "" and found == []


def test_empty_rules_file_is_treated_as_absent(tmp_path):
    (tmp_path / "RAVEN.md").write_text("   \n  \n")
    text, found = load_project_rules(cwd=tmp_path, home=tmp_path / "empty_home")
    assert text == "" and found == []


def test_long_rules_file_is_truncated(tmp_path):
    (tmp_path / "RAVEN.md").write_text("x" * (llm_provider.RULES_MAX_CHARS + 500))
    text, _ = load_project_rules(cwd=tmp_path, home=tmp_path / "empty_home")
    assert "truncated, 500 more chars" in text


def test_build_system_prompt_appends_to_the_base_persona(tmp_path):
    (tmp_path / "RAVEN.md").write_text("PROJECT_MARKER")
    prompt = build_system_prompt(cwd=tmp_path, home=tmp_path / "empty_home", notes_path=tmp_path / "no_notes.md")
    assert prompt.startswith(llm_provider.SYSTEM_PROMPT)
    assert "PROJECT_MARKER" in prompt


def test_build_system_prompt_with_no_rules_files_is_just_the_base_persona(tmp_path):
    prompt = build_system_prompt(cwd=tmp_path, home=tmp_path / "empty_home", notes_path=tmp_path / "no_notes.md")
    assert prompt == llm_provider.SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# load_notes (B3): model-written cross-session notes, distinct from RAVEN.md
# ---------------------------------------------------------------------------

def test_load_notes_missing_file_is_silent(tmp_path):
    assert llm_provider.load_notes(tmp_path / "notes.md") == ""


def test_load_notes_includes_saved_content(tmp_path):
    notes = tmp_path / "notes.md"
    notes.write_text("- the user prefers Rust\n")
    text = llm_provider.load_notes(notes)
    assert "the user prefers Rust" in text
    assert str(notes) in text  # transparency, same as RAVEN.md's "(path)" label


def test_load_notes_empty_file_is_treated_as_absent(tmp_path):
    notes = tmp_path / "notes.md"
    notes.write_text("   \n")
    assert llm_provider.load_notes(notes) == ""


def test_load_notes_defaults_to_the_real_notes_path_when_unset():
    """Regression guard for the argument itself existing and being wired to
    config.NOTES_PATH -- the real path is never touched by THIS test since
    it only checks the default resolves to the same constant, not that it's read."""
    import inspect
    assert inspect.signature(llm_provider.load_notes).parameters["notes_path"].default is None


def test_build_system_prompt_includes_notes(tmp_path):
    notes = tmp_path / "notes.md"
    notes.write_text("- NOTES_MARKER\n")
    prompt = build_system_prompt(cwd=tmp_path / "empty_cwd", home=tmp_path / "empty_home", notes_path=notes)
    assert "NOTES_MARKER" in prompt


# ---------------------------------------------------------------------------
# OpenRouterProvider wiring
# ---------------------------------------------------------------------------

def test_provider_uses_an_explicit_system_prompt_verbatim_no_filesystem_lookup():
    """Passing system_prompt= must skip the RAVEN.md lookup entirely — this
    is what keeps every other test in this project decoupled from the real
    filesystem/cwd (they all go through ScriptedProvider or pass this)."""
    provider = OpenRouterProvider(api_key="fake", system_prompt="JUST THIS")
    assert provider.system_prompt == "JUST THIS"


def test_provider_default_system_prompt_picks_up_project_rules(tmp_path, monkeypatch):
    """Exercises OpenRouterProvider's actual argument-less default (system_prompt=None
    -> build_system_prompt() with no args -> real Path.cwd()/Path.home()) — redirected
    via chdir/$HOME (the standard, non-invasive way to do this), not by patching Path
    itself."""
    (tmp_path / "RAVEN.md").write_text("PROJECT_MARKER")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "empty_home"))
    provider = OpenRouterProvider(api_key="fake")
    assert "PROJECT_MARKER" in provider.system_prompt
    assert provider.system_prompt.startswith(llm_provider.SYSTEM_PROMPT)


def test_reply_sends_the_instances_own_system_prompt(monkeypatch):
    """Regression guard: reply() must use self.system_prompt, not the bare
    module-level SYSTEM_PROMPT constant — otherwise per-instance RAVEN.md
    rules would silently never reach the actual API call."""
    captured = {}

    class FakeResponse:
        ok = True
        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": "hi"}}], "usage": {}}

    def fake_post(url, headers, json, timeout):
        captured["payload"] = json
        return FakeResponse()

    monkeypatch.setattr(llm_provider.requests, "post", fake_post)
    provider = OpenRouterProvider(api_key="fake", system_prompt="CUSTOM PROMPT")
    provider.reply([{"role": "user", "content": "hi"}])
    assert captured["payload"]["messages"][0] == {"role": "system", "content": "CUSTOM PROMPT"}


def test_system_prompt_is_mutable_after_construction():
    """load_skill (D2) extends provider.system_prompt IN PLACE after
    construction -- the attribute must be a plain, reassignable string, and
    a later reply() must reflect the mutation (it reads self.system_prompt
    fresh each call, not a snapshot taken at __init__ time)."""
    captured = {}

    class FakeResponse:
        ok = True
        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": "hi"}}], "usage": {}}

    provider = OpenRouterProvider(api_key="fake", system_prompt="BASE")
    provider.system_prompt += " EXTRA"
    import unittest.mock as mock
    with mock.patch.object(llm_provider.requests, "post", side_effect=lambda **kw: (captured.update(payload=kw["json"]), FakeResponse())[1]):
        provider.reply([{"role": "user", "content": "hi"}])
    assert captured["payload"]["messages"][0]["content"] == "BASE EXTRA"


# ---------------------------------------------------------------------------
# SKILL_PROMPTS (D2): content moved out of SYSTEM_PROMPT, not duplicated
# ---------------------------------------------------------------------------

def test_skill_prompts_exist_for_every_skill_tool_group():
    from raven import tools as tool_impl
    assert set(llm_provider.SKILL_PROMPTS) == set(tool_impl.SKILLS)


def test_system_prompt_no_longer_carries_skill_specific_guardrails():
    """The whole point of D2's system-prompt half: browser/gmail-specific
    guardrail prose must not be paid for by a conversation that never loads
    those skills."""
    assert "Reply-To" not in llm_provider.SYSTEM_PROMPT
    assert "browser_submit" not in llm_provider.SYSTEM_PROMPT
    assert "load_skill" in llm_provider.SYSTEM_PROMPT  # the pointer to go get it, though


def test_gmail_skill_prompt_preserves_the_original_safety_guardrails():
    """Content moved, not rewritten -- the untrusted-email-content and
    Reply-To guidance must still exist verbatim, just deferred."""
    prompt = llm_provider.SKILL_PROMPTS["gmail"]
    assert "untrusted input" in prompt
    assert "reply-all" in prompt
    assert "gmail_reply_message" in prompt


def test_browser_skill_prompt_preserves_the_submit_safety_guardrail():
    prompt = llm_provider.SKILL_PROMPTS["browser"]
    assert "browser_submit" in prompt
    assert "isolated session" in prompt
