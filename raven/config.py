import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

SETTINGS_PATH = Path.home() / ".raven" / "settings.json"

DEFAULT_SETTINGS = {
    # Primary model + fallbacks tried in order when a free model is down/rate-limited.
    "model": {
        "name": "nvidia/nemotron-3.5-lightning:free",
        "fallbacks": ["google/gemma-4-31b-it:free", "openrouter/free"],
    },
    # Status-line segments, in display order. Available names live in statusline.SEGMENTS.
    "statusline": {"segments": ["model", "context", "tokens", "memory"]},
}


def get_api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("Set OPENROUTER_API_KEY in your environment or .env file")
    return key


def load_settings() -> dict:
    """Defaults overlaid with ~/.raven/settings.json (merged one level deep)."""
    settings = {k: dict(v) if isinstance(v, dict) else v for k, v in DEFAULT_SETTINGS.items()}
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
