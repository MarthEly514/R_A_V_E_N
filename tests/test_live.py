"""Tests that hit a real OpenRouter model or a real Google account — genuine
network calls, quota, and (for Gmail) requires completed OAuth setup. Skipped
by default (see pyproject.toml's `addopts = "-m 'not live'"`); run explicitly
with `pytest -m live`.
"""
import os

import pytest

pytestmark = pytest.mark.live


def _has_api_key():
    return bool(os.environ.get("OPENROUTER_API_KEY"))


@pytest.mark.skipif(not _has_api_key(), reason="OPENROUTER_API_KEY not set")
def test_live_plain_reply():
    from raven.config import get_api_key, load_settings
    from raven.llm_provider import OpenRouterProvider
    from raven.assistant import Assistant

    s = load_settings()["model"]
    provider = OpenRouterProvider(api_key=get_api_key(), model=s["name"], fallbacks=s["fallbacks"])
    reply = Assistant(provider).ask("Say READY and nothing else.")
    assert reply.strip()
    assert provider.total_tokens > 0


@pytest.mark.skipif(not _has_api_key(), reason="OPENROUTER_API_KEY not set")
def test_live_model_uses_a_tool_when_the_request_needs_one(tmp_path):
    """A baseline for how reliably the current free models actually call the
    right tool — worth watching over time as models/fallbacks change."""
    from raven.config import get_api_key, load_settings
    from raven.llm_provider import OpenRouterProvider
    from raven.assistant import Assistant

    target = tmp_path / "probe.txt"
    target.write_text("the secret word is banana")
    s = load_settings()["model"]
    provider = OpenRouterProvider(api_key=get_api_key(), model=s["name"], fallbacks=s["fallbacks"])
    seen = []
    a = Assistant(provider, on_tool_call=lambda name, args: seen.append(name))
    reply = a.ask(f"Read the file at {target} and tell me the secret word.")
    assert "read_file" in seen
    assert "banana" in reply.lower()


def _gmail_ready():
    from raven import gmail
    return gmail.CREDENTIALS_PATH.exists()


@pytest.mark.skipif(not _gmail_ready(), reason="Gmail OAuth not set up on this machine")
def test_live_gmail_response_shape_matches_what_the_parser_assumes():
    """Structure only — never prints message content. Caught a real bug once
    (Step 3.28): Gmail returns 'Message-Id', not 'Message-ID'; _header()'s
    case-insensitive lookup covers it, but this guards against that silently
    changing again."""
    from raven import gmail

    service = gmail._get_service()
    resp = service.users().messages().list(userId="me", maxResults=1, q="in:inbox").execute()
    refs = resp.get("messages", [])
    if not refs:
        pytest.skip("inbox is empty")
    message_id = refs[0]["id"]

    orig = service.users().messages().get(
        userId="me", id=message_id, format="metadata",
        metadataHeaders=["From", "Reply-To", "Subject", "Message-ID", "References"],
    ).execute()
    assert orig.get("threadId")

    ctx = gmail._reply_context(message_id)
    assert "@" in ctx["to"]
    assert ctx["subject"].lower().startswith("re:")
    assert ctx["in_reply_to"].startswith("<") and ctx["in_reply_to"].endswith(">")
    assert ctx["thread_id"]
