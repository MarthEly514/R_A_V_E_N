import copy
import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

SETTINGS_PATH = Path.home() / ".raven" / "settings.json"
NOTES_PATH = Path.home() / ".raven" / "memory" / "notes.md"  # model-written, cross-session (B3)

DEFAULT_SETTINGS = {
    # Primary model + fallbacks tried in order when a free model is down/rate-limited.
    "model": {
        # Which LLMProvider subclass build_provider() constructs (llm_provider.py) --
        # "openrouter" or "nvidia" (build.nvidia.com's free-tier NIM API). Each
        # provider reads its own API key from a separate env var -- see
        # PROVIDER_API_KEY_ENV below -- so switching providers doesn't touch
        # whichever key isn't in use.
        "provider": "openrouter",
        "name": "nvidia/nemotron-3.5-lightning:free",
        "fallbacks": ["google/gemma-4-31b-it:free", "openrouter/free"],
        # V2: a SEPARATE model for analyze_image -- the main chat model above
        # isn't necessarily vision-capable, and a model that IS vision-capable
        # isn't necessarily the best (or free) choice for everything else, so
        # this is its own setting rather than reusing model.name.
        #
        # Keyed by PROVIDER (not a single flat string) -- found live
        # (2026-10-09): reply_with_image() always calls out through
        # self.provider, whichever provider model.provider currently names,
        # so a single hardcoded slug silently 404s the instant model.provider
        # is switched to something whose catalog doesn't contain that exact
        # name (confirmed: switching to "nvidia" while this stayed
        # OpenRouter's "qwen/qwen2.5-vl-32b-instruct:free" 404'd on every
        # analyze_image call -- see DEV_LOG Step 3.79, and get_vision_model()
        # below, which resolves this against the ACTIVE provider). Each
        # entry live-confirmed invokable (not just listed) with a real
        # image, not assumed from the catalog alone.
        "vision": {
            "openrouter": "qwen/qwen2.5-vl-32b-instruct:free",
            "nvidia": "meta/llama-3.2-11b-vision-instruct",
            "freellmapi": "qwen/qwen2.5-vl-32b-instruct:free",
        },
    },
    # V2 (release prep): API keys entered through the CLI's first-run
    # onboarding / `/apikey`, or the desktop app's Settings panel -- stored
    # here (not .env) so a fresh install never requires hand-editing a file.
    # Keyed by provider, same keys as PROVIDER_API_KEY_ENV below. An env var
    # still works and is checked as a fallback (see get_api_key) -- this is
    # additive, not a replacement, so an existing .env-based setup is
    # untouched.
    "api_keys": {"openrouter": "", "nvidia": "", "freellmapi": ""},
    # The name R.A.V.E.N calls the user by (CLI startup banner). Empty means
    # "never set" -- see get_user_name and cli.py's onboarding flow, which
    # asks for this once rather than shipping a hardcoded name.
    "user": {"name": ""},
    # Status-line segments, in display order. Available names live in statusline.SEGMENTS.
    "statusline": {"segments": ["model", "context", "tokens", "memory"]},
    # C3: user-opt-in trust, ADDITIVE only — never removes the built-in safety
    # checks (SAFE_COMMANDS/SAFE_GIT, or the shell-metacharacter defeat
    # detection, which always applies regardless of this list). e.g.
    # {"allow_commands": ["pytest"]} lets a plain `pytest` invocation (via
    # run_command or run_tests) skip confirmation; "pytest; rm -rf ~" still
    # never does, since it contains a shell metacharacter.
    "permissions": {"allow_commands": [], "allow_git": []},
    # D1: automation the user pre-approves by putting it in this local file --
    # same trust boundary as permissions.allow_commands, so hooks never ask
    # for confirmation. tool_name -> shell command template, {arg} filled in
    # from that tool call's own arguments, e.g. {"post": {"edit_file": "black {path}"}}
    # to auto-format a file right after a successful edit. Scoped to the
    # model's own tool-calling loop only, not the manual /write, /run, etc.
    # slash commands.
    "hooks": {"pre": {}, "post": {}},
}


# Each provider's API key lives in its own env var, so switching model.provider
# in settings.json never touches a key that isn't currently in use.
PROVIDER_API_KEY_ENV = {
    "openrouter": "OPENROUTER_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    # The UNIFIED bearer token from a self-hosted FreeLLMAPI router's own
    # dashboard (Keys page) -- not any single upstream provider's own key.
    "freellmapi": "FREELLMAPI_API_KEY",
}


def get_api_key(provider: str = "openrouter", settings: dict | None = None) -> str:
    """The API key for `provider` (default "openrouter", so every pre-existing
    no-arg call site keeps working unchanged). Raises a clear, actionable
    RuntimeError -- never returns an empty/missing key silently -- for either
    an unrecognized provider name or a recognized one with no key set.

    `settings` (typically load_settings()'s own return value) is checked
    FIRST -- a key entered via the CLI's /apikey or the desktop app's
    Settings panel (see DEFAULT_SETTINGS["api_keys"]) always wins over an
    env var, so changing it in the UI doesn't require also clearing .env.
    Omitting `settings` (the pre-existing call sites, e.g. tests/test_live.py)
    checks only the environment, same as before this option existed."""
    env_var = PROVIDER_API_KEY_ENV.get(provider)
    if env_var is None:
        raise RuntimeError(
            f"Unknown model provider: {provider!r}. Known providers: "
            f"{', '.join(sorted(PROVIDER_API_KEY_ENV))}."
        )
    if settings is not None:
        key = settings.get("api_keys", {}).get(provider, "").strip()
        if key:
            return key
    key = os.environ.get(env_var)
    if not key:
        raise RuntimeError(
            f"No API key set for {provider!r}. Set it with /apikey in the CLI, "
            f"in Settings in the desktop app, or via {env_var} in your environment/.env file."
        )
    return key


def get_user_name(settings: dict) -> str:
    """The name R.A.V.E.N should call the user by, or "" if never set (see
    DEFAULT_SETTINGS["user"]) -- callers decide their own fallback ("there",
    etc.) rather than this function picking one, since what reads best
    differs between the CLI banner and a GUI greeting."""
    return settings.get("user", {}).get("name", "").strip()


def get_vision_model(settings: dict) -> str | None:
    """The vision-capable model analyze_image should call, for whichever
    provider settings["model"]["provider"] currently names. model.vision can
    be EITHER a dict keyed by provider (the current default -- see its own
    comment in DEFAULT_SETTINGS) OR a single flat string, kept supported here
    so an already-written settings.json from before this change still loads
    and behaves exactly as it did (that string just applies to every
    provider, same as before). None if nothing is configured, or nothing is
    configured for the ACTIVE provider specifically (e.g. a per-provider
    dict with no entry for the one currently selected)."""
    vision = settings.get("model", {}).get("vision")
    if isinstance(vision, dict):
        return vision.get(settings.get("model", {}).get("provider", "openrouter"))
    return vision


def load_settings() -> dict:
    """Defaults overlaid with ~/.raven/settings.json (merged one level deep)."""
    # Deep, not shallow: dict(v) alone only copies one level, so a NESTED list
    # (statusline.segments, model.fallbacks) would still be the same list
    # object as DEFAULT_SETTINGS's — mutating a loaded settings list in place
    # (e.g. .append()) would silently corrupt the module-wide default for the
    # rest of the process. Found by tests/test_config.py, not observed live.
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    if not SETTINGS_PATH.exists():
        return settings
    try:
        overrides = json.loads(SETTINGS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return settings
    for section, value in overrides.items():
        if isinstance(value, dict) and isinstance(settings.get(section), dict):
            settings[section].update(value)
        else:
            settings[section] = value
    return settings


def save_settings(settings: dict) -> None:
    """Persist settings to ~/.raven/settings.json."""
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")
