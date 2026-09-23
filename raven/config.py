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
        "name": "nvidia/nemotron-3.5-lightning:free",
        "fallbacks": ["google/gemma-4-31b-it:free", "openrouter/free"],
    },
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


def get_api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("Set OPENROUTER_API_KEY in your environment or .env file")
    return key


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
