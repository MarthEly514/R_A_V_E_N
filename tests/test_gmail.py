"""raven/gmail.py — parsing, threading, and the injection guards.

All against a fake Gmail service (unittest.mock), never the real API — see
test_gmail_live.py (marked `live`) for the one thing that can only be
verified against the real account.
"""
import base64
import email
from unittest.mock import MagicMock, patch

import pytest

from raven import gmail


def H(name, value):
    return {"name": name, "value": value}


def fake_service(headers, thread_id="THREAD1"):
    svc = MagicMock()
    svc.users().messages().get().execute.return_value = {
        "threadId": thread_id, "payload": {"headers": headers}
    }
    svc.users().messages().send().execute.return_value = {"id": "SENT1"}
    return svc


def sent_body(svc):
    call = svc.users().messages().send.call_args
    return call.kwargs.get("body") if call else None


def decode(body):
    return email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]))


# ---------------------------------------------------------------------------
# Header lookup and body extraction
# ---------------------------------------------------------------------------

def test_header_lookup_is_case_insensitive():
    headers = [H("From", "a@b.com")]
    assert gmail._header(headers, "from") == "a@b.com"
    assert gmail._header(headers, "FROM") == "a@b.com"


def test_header_lookup_missing_returns_empty_string():
    assert gmail._header([], "From") == ""


def test_extract_body_plain_text():
    payload = {"mimeType": "text/plain",
               "body": {"data": base64.urlsafe_b64encode(b"Hello world").decode()}}
    assert gmail._extract_body(payload) == "Hello world"


def test_extract_body_prefers_plain_even_when_html_comes_first_in_the_tree():
    """Regression (Step 3.28): the original walker returned the first text
    part in document order. Multipart emails commonly list their html
    alternative before the plain one, so that returned html instead of plain."""
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(b"<p>HTML</p>").decode()}},
            {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"Plain text body").decode()}},
        ],
    }
    assert gmail._extract_body(payload) == "Plain text body"


def test_extract_body_html_only_fallback_strips_tags():
    payload = {"mimeType": "text/html",
               "body": {"data": base64.urlsafe_b64encode(b"<b>Bold</b> text").decode()}}
    result = gmail._extract_body(payload)
    assert "Bold" in result and "<b>" not in result


def test_extract_body_deeply_nested_multipart_with_an_attachment_sibling():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "multipart/alternative", "parts": [
                {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(b"<p>HTML</p>").decode()}},
                {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"Deeply nested plain").decode()}},
            ]},
            {"mimeType": "application/pdf", "body": {"attachmentId": "xyz"}},
        ],
    }
    assert gmail._extract_body(payload) == "Deeply nested plain"


def test_extract_body_empty_payload():
    assert gmail._extract_body({}) == ""


# ---------------------------------------------------------------------------
# list_messages / read_message: the not-set-up path (real, testable without
# an account) and truncation
# ---------------------------------------------------------------------------

def test_not_set_up_yet_is_a_clear_message_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(gmail, "CREDENTIALS_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(gmail, "_service", None)
    for result in (gmail.list_messages(), gmail.read_message("x"),
                   gmail.send_message("a@b.com", "s", "b"), gmail.reply_message("x", "b")):
        assert "No Gmail credentials" in result


def test_read_message_truncates_a_long_body():
    svc = MagicMock()
    long_body = "x" * (gmail.MAX_BODY_CHARS + 500)
    svc.users().messages().get().execute.return_value = {
        "payload": {
            "headers": [H("From", "a@b.com"), H("Subject", "s"), H("Date", "d")],
            "mimeType": "text/plain",
            "body": {"data": base64.urlsafe_b64encode(long_body.encode()).decode()},
        }
    }
    with patch.object(gmail, "_get_service", return_value=svc):
        result = gmail.read_message("m1")
    assert "(truncated)" in result
    assert len(result) < len(long_body) + 200


# ---------------------------------------------------------------------------
# reply_message: threading, Reply-To, Re: prefixing, References chaining
# ---------------------------------------------------------------------------

def test_reply_threads_correctly():
    svc = fake_service([H("From", "Alice <alice@x.com>"), H("Subject", "Lunch?"), H("Message-ID", "<m1@x>")])
    with patch.object(gmail, "_get_service", return_value=svc):
        out = gmail.reply_message("MSG1", "Sounds good")
    body = sent_body(svc)
    msg = decode(body)
    assert body["threadId"] == "THREAD1"
    assert msg["to"] == "Alice <alice@x.com>"
    assert msg["subject"] == "Re: Lunch?"
    assert msg["In-Reply-To"] == "<m1@x>"
    assert msg["References"] == "<m1@x>"
    assert "Sent reply to Alice" in out


def test_reply_to_beats_from():
    svc = fake_service([H("From", "a@x.com"), H("Reply-To", "replies@x.com"),
                         H("Subject", "S"), H("Message-ID", "<m@x>")])
    with patch.object(gmail, "_get_service", return_value=svc):
        gmail.reply_message("M", "hi")
    assert decode(sent_body(svc))["to"] == "replies@x.com"


@pytest.mark.parametrize("subject", ["Re: Hello", "RE: Hello", "re: Hello"])
def test_reply_does_not_double_the_re_prefix(subject):
    svc = fake_service([H("From", "a@x.com"), H("Subject", subject), H("Message-ID", "<m@x>")])
    with patch.object(gmail, "_get_service", return_value=svc):
        gmail.reply_message("M", "hi")
    assert decode(sent_body(svc))["subject"] == subject


def test_reply_extends_an_existing_references_chain():
    svc = fake_service([H("From", "a@x.com"), H("Subject", "S"), H("Message-ID", "<m3@x>"),
                         H("References", "<m1@x> <m2@x>")])
    with patch.object(gmail, "_get_service", return_value=svc):
        gmail.reply_message("M", "hi")
    assert decode(sent_body(svc))["References"] == "<m1@x> <m2@x> <m3@x>"


def test_reply_preview_shows_the_resolved_recipient_not_the_visible_sender():
    """A sender can set Reply-To to redirect replies elsewhere — the
    confirmation prompt (built from this) must show where mail actually
    goes, not the friendlier-looking visible From address."""
    svc = fake_service([H("From", "Friend <friend@x.com>"), H("Reply-To", "elsewhere@evil.com"),
                         H("Subject", "Hi"), H("Message-ID", "<m@x>")])
    with patch.object(gmail, "_get_service", return_value=svc):
        preview = gmail.reply_preview("M")
    assert "elsewhere@evil.com" in preview
    assert "friend@x.com" not in preview


def test_reply_preview_never_raises_even_when_the_lookup_fails():
    with patch.object(gmail, "_get_service", side_effect=RuntimeError("boom")):
        preview = gmail.reply_preview("M")
    assert "couldn't resolve" in preview.lower()


# ---------------------------------------------------------------------------
# Header-injection hardening: recipients containing a line break are refused
# outright, never flattened into a plausible-looking-but-wrong address
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hostile", [
    "ok@x.com\nBcc: attacker@evil.com",
    "ok@x.com\r\nBcc: attacker@evil.com",
])
def test_reply_refuses_a_hostile_reply_to_and_never_calls_send(hostile):
    svc = fake_service([H("From", "a@x.com"), H("Reply-To", hostile),
                         H("Subject", "S"), H("Message-ID", "<m@x>")])
    with patch.object(gmail, "_get_service", return_value=svc):
        out = gmail.reply_message("M", "hi")
    assert sent_body(svc) is None, "send was called despite a hostile recipient"
    assert out.startswith("Won't reply")


def test_send_message_refuses_a_newline_in_to_and_never_calls_send():
    svc = fake_service([])
    with patch.object(gmail, "_get_service", return_value=svc):
        out = gmail.send_message("a@b.com\nBcc: x@y.com", "Subj", "Body")
    assert sent_body(svc) is None
    assert out.startswith("Couldn't send")


def test_send_message_newline_in_subject_is_flattened_not_refused():
    """Only recipients are refused outright; a subject is just cosmetic and
    is safely flattened instead."""
    svc = fake_service([])
    with patch.object(gmail, "_get_service", return_value=svc):
        gmail.send_message("a@b.com", "Line1\nLine2", "Body")
    assert decode(sent_body(svc))["subject"] == "Line1 Line2"


def test_address_helper_rejects_cr_and_lf():
    with pytest.raises(ValueError):
        gmail._address("a@b.com\nBcc: x")
    with pytest.raises(ValueError):
        gmail._address("a@b.com\rBcc: x")


def test_address_helper_strips_surrounding_whitespace():
    assert gmail._address("  a@b.com  ") == "a@b.com"


# ---------------------------------------------------------------------------
# Insufficient-permission detection (a token that predates the send scope)
# ---------------------------------------------------------------------------

def test_send_reports_insufficient_permission_clearly():
    svc = MagicMock()
    svc.users().messages().send().execute.side_effect = Exception("insufficientPermissions: 403")
    with patch.object(gmail, "_get_service", return_value=svc):
        out = gmail.send_message("a@b.com", "s", "b")
    assert "insufficient Gmail permission" in out
    assert str(gmail.TOKEN_PATH) in out
