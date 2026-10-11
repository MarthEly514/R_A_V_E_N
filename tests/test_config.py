"""raven/config.py — settings.json loading/saving."""
import json

import pytest

from raven import config


def test_defaults_when_no_file(settings_path):
    assert not settings_path.exists()
    settings = config.load_settings()
    assert settings == config.DEFAULT_SETTINGS
    # must be a copy: mutating the result should not corrupt the module default
    settings["statusline"]["segments"].append("x")
    assert "x" not in config.DEFAULT_SETTINGS["statusline"]["segments"]


def test_override_merges_one_level_deep(settings_path):
    settings_path.write_text(json.dumps({"statusline": {"segments": ["model", "cpu"]}}))
    settings = config.load_settings()
    assert settings["statusline"]["segments"] == ["model", "cpu"]
    assert settings["model"] == config.DEFAULT_SETTINGS["model"]  # untouched section survives


def test_malformed_json_falls_back_to_defaults(settings_path):
    settings_path.write_text("{not valid json")
    assert config.load_settings() == config.DEFAULT_SETTINGS


def test_save_then_load_round_trips(settings_path):
    config.save_settings({"statusline": {"segments": ["tokens"]}})
    assert config.load_settings()["statusline"]["segments"] == ["tokens"]


def test_save_creates_parent_directory(tmp_path, monkeypatch):
    nested = tmp_path / "does" / "not" / "exist" / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_PATH", nested)
    config.save_settings({"model": {"name": "x", "fallbacks": []}})
    assert nested.exists()


# ---------------------------------------------------------------------------
# model.provider + get_api_key(provider): multi-provider support (NVIDIA)
# ---------------------------------------------------------------------------

def test_default_model_provider_is_openrouter(settings_path):
    assert config.load_settings()["model"]["provider"] == "openrouter"


def test_model_provider_override_merges_like_any_other_model_field(settings_path):
    settings_path.write_text(json.dumps({"model": {"provider": "nvidia", "name": "meta/llama-3.1-8b-instruct"}}))
    settings = config.load_settings()
    assert settings["model"]["provider"] == "nvidia"
    assert settings["model"]["name"] == "meta/llama-3.1-8b-instruct"
    assert settings["model"]["fallbacks"] == config.DEFAULT_SETTINGS["model"]["fallbacks"]  # untouched


def test_get_api_key_reads_the_right_env_var_per_provider(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("NVIDIA_API_KEY", "nv-key")
    monkeypatch.setenv("FREELLMAPI_API_KEY", "fla-key")
    assert config.get_api_key() == "or-key"  # default, unchanged for every pre-existing call site
    assert config.get_api_key("openrouter") == "or-key"
    assert config.get_api_key("nvidia") == "nv-key"
    assert config.get_api_key("freellmapi") == "fla-key"


def test_get_api_key_missing_env_var_raises_a_clear_message(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="NVIDIA_API_KEY"):
        config.get_api_key("nvidia")


def test_get_api_key_unknown_provider_raises_a_clear_message():
    with pytest.raises(RuntimeError, match="Unknown model provider"):
        config.get_api_key("openai")


# ---------------------------------------------------------------------------
# get_api_key(provider, settings): a key saved via /apikey (CLI) or the
# desktop Settings panel -- settings.json's api_keys, not .env -- release
# goal being that a fresh install never requires hand-editing .env.
# ---------------------------------------------------------------------------

def test_get_api_key_prefers_a_key_saved_in_settings_over_the_env_var(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "env-key")
    settings = {"api_keys": {"nvidia": "settings-key"}}
    assert config.get_api_key("nvidia", settings) == "settings-key"


def test_get_api_key_falls_back_to_env_var_when_settings_has_no_key(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "env-key")
    settings = {"api_keys": {"nvidia": ""}}
    assert config.get_api_key("nvidia", settings) == "env-key"


def test_get_api_key_raises_with_settings_and_no_key_anywhere(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="No API key set"):
        config.get_api_key("nvidia", {"api_keys": {}})


def test_default_settings_has_an_empty_api_key_slot_for_every_known_provider():
    from raven.config import PROVIDER_API_KEY_ENV
    assert set(config.DEFAULT_SETTINGS["api_keys"]) == set(PROVIDER_API_KEY_ENV)
    assert all(v == "" for v in config.DEFAULT_SETTINGS["api_keys"].values())


# ---------------------------------------------------------------------------
# get_user_name: the name R.A.V.E.N calls the user by, replacing a
# hardcoded value -- see cli.py's onboarding flow and /name command.
# ---------------------------------------------------------------------------

def test_get_user_name_is_empty_by_default():
    assert config.get_user_name(config.DEFAULT_SETTINGS) == ""
    assert config.get_user_name({}) == ""


def test_get_user_name_returns_whatever_was_saved():
    assert config.get_user_name({"user": {"name": "Ely"}}) == "Ely"


def test_get_user_name_strips_whitespace_only_names():
    assert config.get_user_name({"user": {"name": "   "}}) == ""


# ---------------------------------------------------------------------------
# permissions (C3): user-configurable, additive trust
# ---------------------------------------------------------------------------

def test_default_permissions_are_empty(settings_path):
    assert config.load_settings()["permissions"] == {"allow_commands": [], "allow_git": []}


def test_permissions_override_merges_like_any_other_section(settings_path):
    settings_path.write_text(json.dumps({"permissions": {"allow_commands": ["pytest"]}}))
    settings = config.load_settings()
    assert settings["permissions"]["allow_commands"] == ["pytest"]


# ---------------------------------------------------------------------------
# hooks (D1): user-configured pre/post automation
# ---------------------------------------------------------------------------

def test_default_hooks_are_empty(settings_path):
    assert config.load_settings()["hooks"] == {"pre": {}, "post": {}}


def test_hooks_override_merges_like_any_other_section(settings_path):
    settings_path.write_text(json.dumps({"hooks": {"post": {"edit_file": "black {path}"}}}))
    settings = config.load_settings()
    assert settings["hooks"]["post"]["edit_file"] == "black {path}"


# ---------------------------------------------------------------------------
# get_vision_model: analyze_image's model, resolved against the ACTIVE
# provider -- see DEFAULT_SETTINGS["model"]["vision"]'s own comment and
# DEV_LOG Step 3.79 for why a single flat slug silently 404s the instant
# model.provider is switched to a provider whose catalog doesn't have it.
# ---------------------------------------------------------------------------

def test_get_vision_model_resolves_against_the_active_provider():
    settings = {
        "model": {
            "provider": "nvidia",
            "vision": {"openrouter": "or-vision", "nvidia": "nv-vision"},
        }
    }
    assert config.get_vision_model(settings) == "nv-vision"


def test_get_vision_model_switches_automatically_when_provider_changes():
    """The exact bug this exists to prevent: switching model.provider must
    never silently keep using a vision model from a DIFFERENT provider's
    catalog."""
    base = {"vision": {"openrouter": "or-vision", "nvidia": "nv-vision"}}
    assert config.get_vision_model({"model": {**base, "provider": "openrouter"}}) == "or-vision"
    assert config.get_vision_model({"model": {**base, "provider": "nvidia"}}) == "nv-vision"


def test_get_vision_model_none_when_active_provider_has_no_entry():
    settings = {"model": {"provider": "freellmapi", "vision": {"nvidia": "nv-vision"}}}
    assert config.get_vision_model(settings) is None


def test_get_vision_model_still_accepts_a_flat_string_for_backward_compatibility():
    """An already-written settings.json from before this change has a plain
    string here -- it must keep working exactly as before (applies to every
    provider), not be silently ignored or crash."""
    settings = {"model": {"provider": "nvidia", "vision": "same-for-everyone"}}
    assert config.get_vision_model(settings) == "same-for-everyone"


def test_get_vision_model_none_when_unset():
    assert config.get_vision_model({"model": {"provider": "nvidia"}}) is None
    assert config.get_vision_model({}) is None


def test_default_settings_has_a_vision_entry_for_every_known_provider():
    """Every provider build_provider() can construct must have SOME default
    vision model, or analyze_image silently can't work out of the box the
    moment a user picks that provider."""
    from raven.config import PROVIDER_API_KEY_ENV
    vision = config.DEFAULT_SETTINGS["model"]["vision"]
    assert set(vision) == set(PROVIDER_API_KEY_ENV)
