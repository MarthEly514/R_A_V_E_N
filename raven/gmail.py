"""Gmail access via the official Gmail API + OAuth, so R.A.V.E.N can answer
questions about the user's real inbox — and, with confirmation, send new
mail or reply to existing mail as them — without browser automation or
screen access (which browser.py's isolated session structurally can't do —
see its docstring).

Scoped to exactly what's needed, nothing broader: gmail.readonly (list/read)
and gmail.send (send only — NOT gmail.modify, so this grant still can't
delete or alter existing mail). This isn't just a policy our own code
enforces — Google's OAuth itself refuses any call outside the granted
scopes, so even a bug here can't turn into an unwanted delete/modify.
Replying needs no extra scope: a reply is just a send that carries a
threadId plus In-Reply-To/References headers. send_message() and
reply_message() both require user confirmation on top of the scope limits,
same as run_command/delete_file/browser_submit.

One-time setup required (can't be done by R.A.V.E.N — needs your Google
account and browser-based consent): see README.md "Gmail access".
"""
import base64
import re
from email.mime.text import MIMEText
from pathlib import Path

AGENT_NAME = "R.A.V.E.N"
SIGNATURE = f"-- \n{AGENT_NAME}"
CREDENTIALS_PATH = Path.home() / ".raven" / "gmail_credentials.json"
TOKEN_PATH = Path.home() / ".raven" / "gmail_token.json"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]
MAX_BODY_CHARS = 4000
_service = None


def _get_service():
    """Build (or reuse) the authenticated Gmail API client. Raises
    RuntimeError with setup instructions if credentials aren't in place yet."""
    global _service
    if _service is not None:
        return _service
    if not CREDENTIALS_PATH.exists():
        raise RuntimeError(
            f"No Gmail credentials at {CREDENTIALS_PATH}. One-time setup needed — "
            f"see README.md 'Gmail access'."
        )
    # Imported lazily: these are only needed once Gmail is actually used, and
    # importing them costs real time (this pulls in google-api-python-client's
    # whole discovery machinery).
    import google_auth_httplib2
    import httplib2
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # First-ever use: opens a browser for the user to log in and
            # consent. Only happens once — the refresh token then covers
            # every run after this until it's revoked or expires.
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json())
    # Pin a socket timeout: Google's client has none by default, so a stalled
    # connection would hang forever — same class of bug as the OpenRouter hang
    # (DEV_LOG Step 3.11), and now reachable from inside the confirmation
    # prompt (reply_preview), where a hang would freeze the whole CLI.
    http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=30))
    _service = build("gmail", "v1", http=http)
    return _service


def _header(headers: list[dict], name: str) -> str:
    return next((h["value"] for h in headers if h["name"].lower() == name.lower()), "")


def _find_part_data(payload: dict, mime_type: str) -> str | None:
    """Search the whole (possibly nested multipart) payload tree for the
    first part matching `mime_type`, return its raw base64 data or None."""
    if payload.get("mimeType") == mime_type and "data" in payload.get("body", {}):
        return payload["body"]["data"]
    for part in payload.get("parts", []) or []:
        found = _find_part_data(part, mime_type)
        if found:
            return found
    return None


def _extract_body(payload: dict) -> str:
    """A message's text: text/plain if the tree has it anywhere, else
    text/html (tags stripped) if that's all there is. Search the whole tree
    for text/plain FIRST, rather than depth-first-first-match — a multipart
    email commonly lists its html alternative before its plain one, and a
    naive first-match walk would return the html part instead."""
    plain = _find_part_data(payload, "text/plain")
    if plain:
        return base64.urlsafe_b64decode(plain).decode("utf-8", errors="replace")
    html = _find_part_data(payload, "text/html")
    if html:
        text = base64.urlsafe_b64decode(html).decode("utf-8", errors="replace")
        return re.sub(r"<[^>]+>", " ", text)
    return ""


def list_messages(query: str = "", max_results: int = 10) -> str:
    """List recent Gmail messages — id, date, sender, subject — optionally
    filtered by a Gmail search query (e.g. "is:unread", "from:someone@x.com",
    "newer_than:7d"). No confirmation needed (read-only)."""
    try:
        service = _get_service()
        resp = service.users().messages().list(
            userId="me", q=query, maxResults=max_results
        ).execute()
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        return f"Couldn't list Gmail messages: {e}"
    refs = resp.get("messages", [])
    if not refs:
        return "(no messages found)"
    lines = []
    for ref in refs:
        msg = service.users().messages().get(
            userId="me", id=ref["id"], format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        headers = msg["payload"]["headers"]
        lines.append(
            f"{ref['id']} | {_header(headers, 'Date')} | "
            f"{_header(headers, 'From')} | {_header(headers, 'Subject')}"
        )
    return "\n".join(lines)


def read_message(message_id: str) -> str:
    """Return one Gmail message's sender, date, subject, and body text, by
    id (from gmail_list_messages). No confirmation needed (read-only)."""
    try:
        service = _get_service()
        msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        return f"Couldn't read message {message_id}: {e}"
    headers = msg["payload"]["headers"]
    body = " ".join(_extract_body(msg["payload"]).split())
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + " … (truncated)"
    return (
        f"From: {_header(headers, 'From')}\n"
        f"Date: {_header(headers, 'Date')}\n"
        f"Subject: {_header(headers, 'Subject')}\n\n{body}"
    )


def _clean(value: str) -> str:
    """Collapse all whitespace (including CR/LF) in a header value. Header
    values come from untrusted places — the model's arguments, or an inbound
    email's own headers — and a newline in one could smuggle in an extra
    header (e.g. a hidden Bcc). The stdlib already refuses that by raising,
    but that's an incidental guard; this makes it explicit and non-fatal."""
    return " ".join(value.split())


def _address(value: str) -> str:
    """A recipient header value. Stricter than _clean: a line break in a
    recipient is never legitimate (the API hands headers back already
    unfolded), and quietly flattening one would leave a mangled string whose
    delivery I can't vouch for ("ok@x.com Bcc: attacker@evil.com"). So refuse
    outright — this changes WHO gets the mail, which is the one thing here
    that must not be guessed at."""
    if "\r" in value or "\n" in value:
        raise ValueError("the recipient contains a line break, which could smuggle in an extra recipient")
    return value.strip()


def _build_mime(to: str, subject: str, body: str) -> MIMEText:
    if SIGNATURE not in body:
        body = f"{body}\n\n{SIGNATURE}"
    mime = MIMEText(body)
    mime["to"] = _address(to)
    mime["subject"] = _clean(subject)
    return mime

# old version
# def _build_mime(to: str, subject: str, body: str) -> MIMEText:
#     mime = MIMEText(body)
#     mime["to"] = _address(to)
#     mime["subject"] = _clean(subject)
#     return mime


def _send_mime(mime: MIMEText, label: str, thread_id: str | None = None) -> str:
    """Send an already-built message. Shared by send_message and reply_message
    so the not-set-up and insufficient-permission handling exist once."""
    try:
        service = _get_service()
    except RuntimeError as e:
        return str(e)
    body = {"raw": base64.urlsafe_b64encode(mime.as_bytes()).decode()}
    if thread_id:
        body["threadId"] = thread_id  # keeps it in the original conversation
    try:
        sent = service.users().messages().send(userId="me", body=body).execute()
    except Exception as e:
        msg = str(e)
        if "insufficient" in msg.lower() or "403" in msg:
            return (
                f"Couldn't send: insufficient Gmail permission (the cached token predates "
                f"the send scope). Delete {TOKEN_PATH} and try again to re-grant access."
            )
        return f"Couldn't send {label}: {e}"
    return f"Sent {label} to {mime['to']} (id {sent.get('id', '?')})."


def send_message(to: str, subject: str, body: str) -> str:
    """Send a NEW email as the user (starts a new thread — use reply_message
    to answer an existing one). Requires user confirmation."""
    try:
        mime = _build_mime(to, subject, body)
    except ValueError as e:
        return f"Couldn't send: {e}."
    return _send_mime(mime, "email")


def _reply_context(message_id: str) -> dict:
    """Resolve what a reply needs from the original message: who it goes to,
    the subject, and the threading headers. Shared by reply_preview (what the
    confirmation prompt shows) and reply_message (what actually gets sent), so
    the preview can never show a different recipient than the send uses.
    Raises on any failure (including RuntimeError when Gmail isn't set up)."""
    service = _get_service()
    orig = service.users().messages().get(
        userId="me", id=message_id, format="metadata",
        metadataHeaders=["From", "Reply-To", "Subject", "Message-ID", "References"],
    ).execute()
    headers = orig["payload"]["headers"]
    subject = _clean(_header(headers, "Subject"))
    original_id = _clean(_header(headers, "Message-ID"))
    references = _clean(_header(headers, "References"))
    return {
        # Reply-To wins over From, as in any mail client — which also means a
        # sender can point replies elsewhere; the confirmation preview shows
        # this resolved address, not the visible sender, for that reason.
        "to": _address(_header(headers, "Reply-To") or _header(headers, "From")),
        "subject": subject if subject.lower().startswith("re:") else f"Re: {subject}",
        "thread_id": orig["threadId"],
        "in_reply_to": original_id,
        "references": f"{references} {original_id}".strip(),
    }


def reply_preview(message_id: str) -> str:
    """The To/Subject a reply to `message_id` would use, for the confirmation
    prompt. Never raises: if the lookup fails the prompt still appears (a
    failed lookup must not silently skip confirmation), just with a note."""
    try:
        ctx = _reply_context(message_id)
    except Exception as e:
        return f"To: (couldn't resolve the recipient: {e})"
    return f"To: {ctx['to']}\n  Subject: {ctx['subject']}"


def reply_message(message_id: str, body: str) -> str:
    """Reply to an existing email, in its thread, to whoever it says to reply
    to. Plain reply only — not reply-all. Requires user confirmation."""
    try:
        ctx = _reply_context(message_id)
    except RuntimeError as e:
        return str(e)
    except ValueError as e:
        return f"Won't reply: {e}."
    except Exception as e:
        return f"Couldn't read message {message_id} to reply to it: {e}"
    mime = _build_mime(ctx["to"], ctx["subject"], body)
    if ctx["in_reply_to"]:
        mime["In-Reply-To"] = ctx["in_reply_to"]
    if ctx["references"]:
        mime["References"] = ctx["references"]
    return _send_mime(mime, "reply", thread_id=ctx["thread_id"])
