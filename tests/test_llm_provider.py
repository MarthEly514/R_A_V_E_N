"""raven/llm_provider.py — RAVEN.md rules loading (B1) and OpenRouterProvider
wiring. cwd/home are always passed explicitly here (never real Path.cwd()/
Path.home()) so this suite can never accidentally pick up a real RAVEN.md."""
import pytest

from raven import llm_provider
from raven.llm_provider import (
    NvidiaProvider, OpenRouterProvider, FreeLLMAPIProvider, FREELLMAPI_DEFAULT_BASE_URL,
    build_system_prompt, list_nvidia_models, list_freellmapi_models, load_project_rules,
)


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


def test_build_system_prompt_with_no_rules_files_is_just_the_base_persona(tmp_path, monkeypatch):
    """Found via real CI (Windows): this asserted exact equality with no OS
    note, but _os_note() genuinely detects Windows/macOS when actually running
    ON one -- forcing "Linux" here makes the assertion OS-independent, same
    as test_platforms.py's own pattern for exercising one OS deterministically
    regardless of which OS pytest happens to run on."""
    from raven import platforms
    monkeypatch.setattr(platforms, "is_windows", lambda: False)
    monkeypatch.setattr(platforms, "is_macos", lambda: False)
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

    # Patched on the provider's OWN Session instance (not the requests module) --
    # each provider now keeps one persistent Session for connection reuse, so
    # that's what actually issues the call. See OpenRouterProvider.__init__.
    provider = OpenRouterProvider(api_key="fake", system_prompt="CUSTOM PROMPT")
    monkeypatch.setattr(provider._session, "post", fake_post)
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
    with mock.patch.object(provider._session, "post", side_effect=lambda **kw: (captured.update(payload=kw["json"]), FakeResponse())[1]):
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


# ---------------------------------------------------------------------------
# reply_with_image (V2): a separate, single-shot multimodal call
# ---------------------------------------------------------------------------

def test_reply_with_image_builds_the_multimodal_payload_and_uses_the_given_model(monkeypatch):
    captured = {}

    class FakeResponse:
        ok = True
        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": "a red circle"}}], "usage": {}}

    def fake_post(url, headers, json, timeout):
        captured["payload"] = json
        return FakeResponse()

    provider = OpenRouterProvider(api_key="fake", model="text-only-model", system_prompt="BASE")
    monkeypatch.setattr(provider._session, "post", fake_post)
    result = provider.reply_with_image("what is this?", "ZmFrZWJhc2U2NA==", "image/png", "vision-model")

    assert result == "a red circle"
    payload = captured["payload"]
    assert payload["models"] == ["vision-model"]  # the GIVEN model, not provider.model
    content = payload["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "what is this?"}
    assert content[1]["image_url"]["url"] == "data:image/png;base64,ZmFrZWJhc2U2NA=="
    # no system prompt at all in this call -- a single-shot analysis request,
    # not part of R.A.V.E.N's own persona/history (same reasoning _compact() applies)
    assert not any(m.get("role") == "system" for m in payload["messages"])


def test_reply_with_image_updates_token_and_model_bookkeeping(monkeypatch):
    class FakeResponse:
        ok = True
        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": "x"}}],
                     "usage": {"total_tokens": 42}, "model": "vision-model"}

    provider = OpenRouterProvider(api_key="fake", system_prompt="BASE")
    monkeypatch.setattr(provider._session, "post", lambda **kw: FakeResponse())
    provider.reply_with_image("q", "aGk=", "image/png", "vision-model")
    assert provider.total_tokens == 42
    assert provider.last_model == "vision-model"


def test_reply_with_image_propagates_a_failed_call(monkeypatch):
    class FakeResponse:
        ok = False
        status_code = 429
        text = "rate limited"

    provider = OpenRouterProvider(api_key="fake", system_prompt="BASE")
    monkeypatch.setattr(provider._session, "post", lambda **kw: FakeResponse())
    with pytest.raises(RuntimeError, match="429"):
        provider.reply_with_image("q", "aGk=", "image/png", "vision-model")


def test_llm_provider_base_class_declares_reply_with_image_as_a_typed_but_unsupported_default():
    """Regression guard for the same static-typing class of issue system_prompt
    had: self.provider.reply_with_image must be a valid attribute access for
    ANY LLMProvider subclass, not just OpenRouterProvider -- a subclass that
    doesn't override it should still have a callable method (raising a
    clear NotImplementedError), not an AttributeError from a missing method."""
    class MinimalProvider(llm_provider.LLMProvider):
        def reply(self, messages, tools=None):
            return {"role": "assistant", "content": "hi"}

    provider = MinimalProvider()
    assert hasattr(provider, "reply_with_image")  # inherited from the ABC, always true
    with pytest.raises(NotImplementedError, match="MinimalProvider does not support image analysis"):
        provider.reply_with_image("q", "aGk=", "image/png", "vision-model")


# ---------------------------------------------------------------------------
# NvidiaProvider: an OpenAI-compatible endpoint, single "model" field (no
# OpenRouter-style multi-model request), client-side fallback instead.
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._payload


def _chat_response(content="hi", model="m1", total_tokens=7):
    return _FakeResponse(200, {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"total_tokens": total_tokens},
        "model": model,
    })


def test_nvidia_reply_sends_a_single_model_field_not_a_models_list(monkeypatch):
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured["url"], captured["headers"], captured["payload"] = url, headers, json
        return _chat_response()

    provider = NvidiaProvider(api_key="nv-key", model="meta/llama-3.1-8b-instruct", system_prompt="SYS")
    monkeypatch.setattr(provider._session, "post", fake_post)
    provider.reply([{"role": "user", "content": "hi"}])

    assert captured["url"] == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer nv-key"
    assert captured["payload"]["model"] == "meta/llama-3.1-8b-instruct"
    assert "models" not in captured["payload"]  # NOT OpenRouter's multi-model extension
    assert captured["payload"]["messages"][0] == {"role": "system", "content": "SYS"}


def test_nvidia_reply_updates_token_and_model_bookkeeping(monkeypatch):
    provider = NvidiaProvider(api_key="k", model="meta/llama-3.1-8b-instruct", system_prompt="SYS")
    monkeypatch.setattr(provider._session, "post", lambda **kw: _chat_response(model="meta/llama-3.1-8b-instruct", total_tokens=12))
    provider.reply([{"role": "user", "content": "hi"}])
    assert provider.total_tokens == 12
    assert provider.last_model == "meta/llama-3.1-8b-instruct"


def test_nvidia_falls_back_to_the_next_model_on_failure(monkeypatch):
    """Client-side fallback: this endpoint has no single-request "try these
    models in order" field, so NvidiaProvider must retry with each fallback
    itself, one HTTP call per candidate, stopping at the first success."""
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append(json["model"])
        if json["model"] == "primary":
            return _FakeResponse(503, text="model overloaded")
        return _chat_response(model=json["model"])

    provider = NvidiaProvider(api_key="k", model="primary", fallbacks=["backup"], system_prompt="SYS")
    monkeypatch.setattr(provider._session, "post", fake_post)
    result = provider.reply([{"role": "user", "content": "hi"}])

    assert calls == ["primary", "backup"]
    assert result == {"role": "assistant", "content": "hi"}
    assert provider.last_model == "backup"


def test_nvidia_raises_the_last_error_when_every_candidate_fails(monkeypatch):
    provider = NvidiaProvider(api_key="k", model="primary", fallbacks=["backup"], system_prompt="SYS")
    monkeypatch.setattr(provider._session, "post", lambda **kw: _FakeResponse(500, text="boom"))
    with pytest.raises(RuntimeError, match="500"):
        provider.reply([{"role": "user", "content": "hi"}])


def test_nvidia_translates_the_not_enabled_for_account_404_into_an_actionable_message(monkeypatch):
    """Found live (2026-09-28): NVIDIA's catalog can list a model that a given
    key's account isn't actually entitled to call -- surfaced as this exact
    404 wording, decoded here rather than left as an opaque function UUID."""
    body = '{"status":404,"title":"Not Found","detail":"Function \'abc-123\': Not found for account \'xyz\'"}'
    provider = NvidiaProvider(api_key="k", model="some/model", system_prompt="SYS")
    monkeypatch.setattr(provider._session, "post", lambda **kw: _FakeResponse(404, text=body))
    with pytest.raises(RuntimeError, match="isn't enabled for this API key's account"):
        provider.reply([{"role": "user", "content": "hi"}])


def test_nvidia_an_ordinary_404_is_not_mistaken_for_the_not_enabled_case(monkeypatch):
    provider = NvidiaProvider(api_key="k", model="some/model", system_prompt="SYS")
    monkeypatch.setattr(provider._session, "post", lambda **kw: _FakeResponse(404, text="404 page not found"))
    with pytest.raises(RuntimeError, match="^NVIDIA 404: 404 page not found$"):
        provider.reply([{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# Timeouts/connection failures: found live (2026-09-28) -- these leaked the
# raw requests/urllib3 exception text straight into the UI's error banner
# ("HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read
# timed out. (read timeout=60)"), which tells a user nothing actionable.
# ---------------------------------------------------------------------------

def _raise(exc):
    def _fn(**kw):
        raise exc
    return _fn


def test_nvidia_a_read_timeout_is_translated_to_a_clear_actionable_message(monkeypatch):
    import requests as real_requests
    provider = NvidiaProvider(api_key="k", model="some/model", system_prompt="SYS")
    monkeypatch.setattr(provider._session, "post", _raise(real_requests.exceptions.ReadTimeout("Read timed out. (read timeout=60)")))
    with pytest.raises(RuntimeError, match="didn't respond within 60s"):
        provider.reply([{"role": "user", "content": "hi"}])


def test_nvidia_a_connection_error_is_translated_clearly(monkeypatch):
    import requests as real_requests
    provider = NvidiaProvider(api_key="k", model="some/model", system_prompt="SYS")
    monkeypatch.setattr(provider._session, "post", _raise(real_requests.exceptions.ConnectionError("no route to host")))
    with pytest.raises(RuntimeError, match="Couldn't reach NVIDIA"):
        provider.reply([{"role": "user", "content": "hi"}])


def test_nvidia_a_timeout_on_the_primary_still_falls_back(monkeypatch):
    """The translated RuntimeError must still be an ordinary exception
    _post_with_fallback can catch and move past, same as any other failure."""
    import requests as real_requests
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append(json["model"])
        if json["model"] == "primary":
            raise real_requests.exceptions.ReadTimeout("Read timed out.")
        return _chat_response(model=json["model"])

    provider = NvidiaProvider(api_key="k", model="primary", fallbacks=["backup"], system_prompt="SYS")
    monkeypatch.setattr(provider._session, "post", fake_post)
    result = provider.reply([{"role": "user", "content": "hi"}])

    assert calls == ["primary", "backup"]
    assert result == {"role": "assistant", "content": "hi"}


def test_openrouter_a_read_timeout_is_translated_to_a_clear_actionable_message(monkeypatch):
    import requests as real_requests
    provider = OpenRouterProvider(api_key="fake", system_prompt="BASE")
    monkeypatch.setattr(provider._session, "post", _raise(real_requests.exceptions.ReadTimeout("Read timed out. (read timeout=60)")))
    with pytest.raises(RuntimeError, match="OpenRouter didn't respond within 60s"):
        provider.reply([{"role": "user", "content": "hi"}])


def test_openrouter_a_connection_error_is_translated_clearly(monkeypatch):
    import requests as real_requests
    provider = OpenRouterProvider(api_key="fake", system_prompt="BASE")
    monkeypatch.setattr(provider._session, "post", _raise(real_requests.exceptions.ConnectionError("no route to host")))
    with pytest.raises(RuntimeError, match="Couldn't reach OpenRouter"):
        provider.reply([{"role": "user", "content": "hi"}])


def test_nvidia_reply_with_image_uses_the_given_model_not_the_chat_model(monkeypatch):
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured["payload"] = json
        return _chat_response(content="a red circle")

    provider = NvidiaProvider(api_key="k", model="chat-model", system_prompt="SYS")
    monkeypatch.setattr(provider._session, "post", fake_post)
    result = provider.reply_with_image("what is this", "aGk=", "image/png", "vision-model")

    assert result == "a red circle"
    assert captured["payload"]["model"] == "vision-model"
    assert captured["payload"]["messages"][0]["content"][1] == {
        "type": "image_url", "image_url": {"url": "data:image/png;base64,aGk="},
    }
    assert "system" not in [m["role"] for m in captured["payload"]["messages"]]


def test_nvidia_is_a_real_llm_provider_subclass():
    provider = NvidiaProvider(api_key="k", model="m", system_prompt="SYS")
    assert isinstance(provider, llm_provider.LLMProvider)


# ---------------------------------------------------------------------------
# list_nvidia_models: powers the desktop UI's provider/model picker
# ---------------------------------------------------------------------------

def test_list_nvidia_models_returns_sorted_ids(monkeypatch):
    def fake_get(url, headers, timeout):
        assert url == "https://integrate.api.nvidia.com/v1/models"
        assert headers["Authorization"] == "Bearer nv-key"
        return _FakeResponse(200, {"data": [{"id": "meta/llama-3.1-70b"}, {"id": "google/gemma-2-9b"}]})

    monkeypatch.setattr(llm_provider.requests, "get", fake_get)
    assert list_nvidia_models("nv-key") == ["google/gemma-2-9b", "meta/llama-3.1-70b"]


def test_list_nvidia_models_reports_a_bad_key_clearly(monkeypatch):
    monkeypatch.setattr(llm_provider.requests, "get", lambda **kw: _FakeResponse(401, text="Unauthorized"))
    with pytest.raises(RuntimeError, match="401"):
        list_nvidia_models("bad-key")


def test_list_nvidia_models_reports_a_network_failure_clearly(monkeypatch):
    import requests as real_requests

    def raise_it(**kw):
        raise real_requests.ConnectionError("no route to host")

    monkeypatch.setattr(llm_provider.requests, "get", raise_it)
    with pytest.raises(RuntimeError, match="no route to host"):
        list_nvidia_models("nv-key")


def test_list_nvidia_models_reports_an_unexpected_shape_clearly(monkeypatch):
    monkeypatch.setattr(llm_provider.requests, "get", lambda **kw: _FakeResponse(200, {"unexpected": "shape"}))
    with pytest.raises(RuntimeError, match="Unexpected response"):
        list_nvidia_models("nv-key")


# ---------------------------------------------------------------------------
# FreeLLMAPIProvider: a self-hosted router (github.com/tashfeenahmed/freellmapi)
# pooling many free providers behind one OpenAI-compatible endpoint, with its
# own failover/malformed-tool-call rescue -- see Step in DEV_LOG for why.
# ---------------------------------------------------------------------------

def test_freellmapi_defaults_to_auto_and_the_installer_default_base_url():
    provider = FreeLLMAPIProvider(api_key="k", system_prompt="SYS")
    assert provider.model == "auto"
    assert provider.base_url == FREELLMAPI_DEFAULT_BASE_URL


def test_freellmapi_reply_sends_a_single_model_field_to_the_configured_base_url(monkeypatch):
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured["url"], captured["headers"], captured["payload"] = url, headers, json
        return _chat_response()

    provider = FreeLLMAPIProvider(
        api_key="fla-key", model="auto", system_prompt="SYS", base_url="http://localhost:3002/v1",
    )
    monkeypatch.setattr(provider._session, "post", fake_post)
    provider.reply([{"role": "user", "content": "hi"}])

    assert captured["url"] == "http://localhost:3002/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer fla-key"
    assert captured["payload"]["model"] == "auto"
    assert "models" not in captured["payload"]  # not OpenRouter's multi-model extension
    assert captured["payload"]["messages"][0] == {"role": "system", "content": "SYS"}


def test_freellmapi_base_url_trailing_slash_is_tolerated(monkeypatch):
    captured = {}
    provider = FreeLLMAPIProvider(api_key="k", system_prompt="SYS", base_url="http://localhost:3002/v1/")
    monkeypatch.setattr(provider._session, "post", lambda **kw: (captured.update(kw) or _chat_response()))
    provider.reply([{"role": "user", "content": "hi"}])
    assert captured["url"] == "http://localhost:3002/v1/chat/completions"


def test_freellmapi_prefers_the_x_routed_via_header_for_last_model(monkeypatch):
    """The router's own header says which REAL provider/model actually
    served a model="auto" request -- far more useful for the statusline
    than echoing back the literal "auto" that was sent."""
    provider = FreeLLMAPIProvider(api_key="k", model="auto", system_prompt="SYS")
    response = _chat_response(model="auto")
    response.headers = {"X-Routed-Via": "nvidia/meta-llama-3.2-11b-vision-instruct"}
    monkeypatch.setattr(provider._session, "post", lambda **kw: response)
    provider.reply([{"role": "user", "content": "hi"}])
    assert provider.last_model == "nvidia/meta-llama-3.2-11b-vision-instruct"


def test_freellmapi_falls_back_to_response_model_without_the_header(monkeypatch):
    provider = FreeLLMAPIProvider(api_key="k", model="auto", system_prompt="SYS")
    monkeypatch.setattr(provider._session, "post", lambda **kw: _chat_response(model="some/model"))
    provider.reply([{"role": "user", "content": "hi"}])
    assert provider.last_model == "some/model"


def test_freellmapi_reply_updates_token_bookkeeping(monkeypatch):
    provider = FreeLLMAPIProvider(api_key="k", system_prompt="SYS")
    monkeypatch.setattr(provider._session, "post", lambda **kw: _chat_response(total_tokens=42))
    provider.reply([{"role": "user", "content": "hi"}])
    assert provider.total_tokens == 42


def test_freellmapi_fallbacks_are_accepted_but_never_sent():
    """FreeLLMAPI's own fallback chain lives in ITS dashboard, not the
    request -- fallbacks is accepted only for constructor-shape parity with
    OpenRouterProvider/NvidiaProvider."""
    provider = FreeLLMAPIProvider(api_key="k", fallbacks=["some/model"], system_prompt="SYS")
    assert provider.fallbacks == ["some/model"]


def test_freellmapi_a_failure_raises_with_a_clear_message(monkeypatch):
    provider = FreeLLMAPIProvider(api_key="k", system_prompt="SYS")
    monkeypatch.setattr(provider._session, "post", lambda **kw: _FakeResponse(500, text="boom"))
    with pytest.raises(RuntimeError, match="FreeLLMAPI 500: boom"):
        provider.reply([{"role": "user", "content": "hi"}])


def test_freellmapi_reply_with_image_uses_the_given_model(monkeypatch):
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured["payload"] = json
        return _chat_response(content="a red circle")

    provider = FreeLLMAPIProvider(api_key="k", system_prompt="SYS")
    monkeypatch.setattr(provider._session, "post", fake_post)
    result = provider.reply_with_image("what is this", "aGk=", "image/png", "vision-model")

    assert result == "a red circle"
    assert captured["payload"]["model"] == "vision-model"
    assert captured["payload"]["messages"][0]["content"][1] == {
        "type": "image_url", "image_url": {"url": "data:image/png;base64,aGk="},
    }


def test_freellmapi_is_a_real_llm_provider_subclass():
    provider = FreeLLMAPIProvider(api_key="k", system_prompt="SYS")
    assert isinstance(provider, llm_provider.LLMProvider)


def test_freellmapi_is_in_known_providers():
    assert "freellmapi" in llm_provider.KNOWN_PROVIDERS


# ---------------------------------------------------------------------------
# list_freellmapi_models
# ---------------------------------------------------------------------------

def test_list_freellmapi_models_returns_sorted_ids(monkeypatch):
    def fake_get(url, headers, timeout):
        assert url == "http://localhost:3002/v1/models"
        assert headers["Authorization"] == "Bearer fla-key"
        return _FakeResponse(200, {"data": [{"id": "auto"}, {"id": "nvidia/some-model"}]})

    monkeypatch.setattr(llm_provider.requests, "get", fake_get)
    assert list_freellmapi_models("fla-key", "http://localhost:3002/v1") == ["auto", "nvidia/some-model"]


def test_list_freellmapi_models_reports_a_connection_failure_clearly(monkeypatch):
    import requests as real_requests

    def raise_it(**kw):
        raise real_requests.ConnectionError("connection refused")

    monkeypatch.setattr(llm_provider.requests, "get", raise_it)
    with pytest.raises(RuntimeError, match="Couldn't reach FreeLLMAPI"):
        list_freellmapi_models("fla-key")
