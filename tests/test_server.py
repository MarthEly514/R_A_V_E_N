"""raven/server.py — the WebSocket bridge wrapping Assistant for the
desktop UI. Every test mocks get_api_key/OpenRouterProvider/Store (same
discipline as test_cli.py's build_assistant tests) so nothing here ever
touches a real API key, makes a real network call, or writes to the real
~/.raven/history.db. FastAPI's TestClient drives the real ASGI app
in-process (a real WebSocket handshake and message loop, not a mock of the
server itself) -- only the Assistant's own dependencies are faked.
"""
import json
import threading
from unittest.mock import ANY, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from raven import server


@pytest.fixture(autouse=True)
def _isolate_settings(settings_path):
    """Found live (2026-09-28): _build_assistant calls the REAL load_settings()
    in every test here that doesn't explicitly mock it -- meaning any test
    that assumes OpenRouterProvider gets constructed was silently depending
    on the real ~/.raven/settings.json NOT saying model.provider: "nvidia".
    Once something (this project's own live model-switch testing, or a user
    clicking "Use this model" in the real app) actually set that, those tests
    started constructing a REAL NvidiaProvider and making a REAL network call
    with the mocked "fake-key" -- a genuine 401, not the mocked reply. autouse
    so every test in this file is isolated by default, the same way `store`
    already isolates ~/.raven/history.db; a test that wants specific settings
    still patches `load_settings` directly (see the model-config tests) --
    that patch simply wins over this file staying absent/default."""
    return settings_path


def _configure(MockStore, history=None):
    """A mocked Store that behaves like an empty single-session store."""
    m = MockStore.return_value
    m.load.return_value = history or []
    m.load_canvas.return_value = []
    m.session_id = 1
    m.list_sessions.return_value = []
    m.message_count.return_value = 0


def _patched():
    """Context manager stack shorthand: get_api_key/OpenRouterProvider/Store
    all mocked, Store().load() returns an empty history (a fresh session)."""
    return (
        patch.object(server, "get_api_key", return_value="fake-key"),
        patch.object(server, "OpenRouterProvider"),
        patch.object(server, "Store"),
    )


def test_ask_returns_a_reply_over_the_websocket():
    with patch.object(server, "get_api_key", return_value="fake-key"), \
         patch.object(server, "OpenRouterProvider") as MockProvider, \
         patch.object(server, "Store") as MockStore:
        _configure(MockStore)
        MockProvider.return_value.reply.return_value = {"role": "assistant", "content": "hello there"}
        client = TestClient(server.app)
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "ask", "text": "hi"})
            msg = ws.receive_json()
    assert msg == {"type": "reply", "text": "hello there"}


def test_tool_call_notification_is_sent_before_the_reply():
    with patch.object(server, "get_api_key", return_value="fake-key"), \
         patch.object(server, "OpenRouterProvider") as MockProvider, \
         patch.object(server, "Store") as MockStore:
        _configure(MockStore)
        MockProvider.return_value.reply.side_effect = [
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": '{"path": "x"}'}}]},
            {"role": "assistant", "content": "the file says hi"},
        ]
        with patch("raven.tools.TOOL_FUNCTIONS", {**__import__("raven.tools", fromlist=["x"]).TOOL_FUNCTIONS,
                                                  "read_file": lambda path: "hi"}):
            client = TestClient(server.app)
            with client.websocket_connect("/ws") as ws:
                ws.send_json({"type": "ask", "text": "read x"})
                tool_msg = ws.receive_json()
                reply_msg = ws.receive_json()
    assert tool_msg == {"type": "tool_call", "name": "read_file", "args": {"path": "x"}}
    assert reply_msg == {"type": "reply", "text": "the file says hi"}


def test_confirmation_round_trip_approved():
    """The architecturally important path: a tool needing confirmation
    (delete_file) sends a confirm_request and genuinely BLOCKS the
    background ask() thread until confirm_response arrives, then proceeds."""
    with patch.object(server, "get_api_key", return_value="fake-key"), \
         patch.object(server, "OpenRouterProvider") as MockProvider, \
         patch.object(server, "Store") as MockStore:
        _configure(MockStore)
        MockProvider.return_value.reply.side_effect = [
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "c1", "function": {"name": "delete_file", "arguments": '{"path": "x.txt"}'}}]},
            {"role": "assistant", "content": "Deleted x.txt"},
        ]
        with patch("raven.tools.TOOL_FUNCTIONS", {**__import__("raven.tools", fromlist=["x"]).TOOL_FUNCTIONS,
                                                  "delete_file": lambda path: f"Deleted {path}"}):
            client = TestClient(server.app)
            with client.websocket_connect("/ws") as ws:
                ws.send_json({"type": "ask", "text": "delete x.txt"})
                # Assistant always fires on_tool_call before checking confirmation
                # (verified elsewhere in this project) -- tool_call arrives first.
                tool_msg = ws.receive_json()
                assert tool_msg == {"type": "tool_call", "name": "delete_file", "args": {"path": "x.txt"}}
                confirm_msg = ws.receive_json()
                assert confirm_msg["type"] == "confirm_request"
                assert "x.txt" in confirm_msg["prompt"]
                assert confirm_msg["id"]
                ws.send_json({"type": "confirm_response", "id": confirm_msg["id"], "approved": True})
                reply_msg = ws.receive_json()
    assert reply_msg == {"type": "reply", "text": "Deleted x.txt"}


def test_confirmation_round_trip_denied():
    with patch.object(server, "get_api_key", return_value="fake-key"), \
         patch.object(server, "OpenRouterProvider") as MockProvider, \
         patch.object(server, "Store") as MockStore:
        _configure(MockStore)
        MockProvider.return_value.reply.side_effect = [
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "c1", "function": {"name": "delete_file", "arguments": '{"path": "x.txt"}'}}]},
            {"role": "assistant", "content": "I did not delete the file since you denied it."},
        ]
        deleted = []
        with patch("raven.tools.TOOL_FUNCTIONS", {**__import__("raven.tools", fromlist=["x"]).TOOL_FUNCTIONS,
                                                  "delete_file": lambda path: deleted.append(path)}):
            client = TestClient(server.app)
            with client.websocket_connect("/ws") as ws:
                ws.send_json({"type": "ask", "text": "delete x.txt"})
                ws.receive_json()  # tool_call notification, ignored here
                confirm_msg = ws.receive_json()
                ws.send_json({"type": "confirm_response", "id": confirm_msg["id"], "approved": False})
                reply_msg = ws.receive_json()
    assert deleted == []  # the tool function itself was never called
    assert "did not delete" in reply_msg["text"]


def test_invalid_json_reports_an_error_without_crashing_the_connection():
    with patch.object(server, "get_api_key", return_value="fake-key"), \
         patch.object(server, "OpenRouterProvider") as MockProvider, \
         patch.object(server, "Store") as MockStore:
        _configure(MockStore)
        client = TestClient(server.app)
        with client.websocket_connect("/ws") as ws:
            ws.send_text("not valid json{{{")
            msg = ws.receive_json()
    assert msg["type"] == "error"


def test_unknown_message_type_reports_an_error():
    with patch.object(server, "get_api_key", return_value="fake-key"), \
         patch.object(server, "OpenRouterProvider") as MockProvider, \
         patch.object(server, "Store") as MockStore:
        _configure(MockStore)
        client = TestClient(server.app)
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "not_a_real_type"})
            msg = ws.receive_json()
    assert msg["type"] == "error"


def test_ask_failure_is_reported_as_an_error_message_not_a_crash():
    with patch.object(server, "get_api_key", return_value="fake-key"), \
         patch.object(server, "OpenRouterProvider") as MockProvider, \
         patch.object(server, "Store") as MockStore:
        _configure(MockStore)
        MockProvider.return_value.reply.side_effect = RuntimeError("OpenRouter 429: rate limited")
        client = TestClient(server.app)
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "ask", "text": "hi"})
            msg = ws.receive_json()
    assert msg["type"] == "error"
    assert "rate limited" in msg["message"]


def test_each_connection_gets_a_fresh_session_with_its_own_assistant():
    with patch.object(server, "get_api_key", return_value="fake-key"), \
         patch.object(server, "OpenRouterProvider") as MockProvider, \
         patch.object(server, "Store") as MockStore:
        _configure(MockStore)
        MockProvider.return_value.reply.return_value = {"role": "assistant", "content": "hi"}
        client = TestClient(server.app)
        with client.websocket_connect("/ws") as ws1:
            ws1.send_json({"type": "ask", "text": "hi"})
            ws1.receive_json()
        with client.websocket_connect("/ws") as ws2:
            ws2.send_json({"type": "ask", "text": "hi"})
            ws2.receive_json()
    # both connections succeeded independently -- no shared/broken state between them
    assert MockProvider.call_count == 2


# ---------------------------------------------------------------------------
# Session.resolve_all_pending: unblocks a hanging confirm_run on disconnect,
# tested directly (isolated from real disconnect timing, which is hard to
# control deterministically through a full WebSocket round trip).
# ---------------------------------------------------------------------------

def test_resolve_all_pending_denies_by_default():
    import threading
    with patch.object(server, "get_api_key", return_value="fake-key"), \
         patch.object(server, "OpenRouterProvider"), \
         patch.object(server, "Store") as MockStore:
        _configure(MockStore)
        session = server.Session(websocket=MagicMock(), loop=MagicMock())
        event = threading.Event()
        answer_box = {"approved": True}  # deliberately wrong, to prove resolve_all_pending overwrites it
        session._pending_confirms["fake-id"] = (event, answer_box)
        session.resolve_all_pending(approved=False)
    assert event.is_set()
    assert answer_box["approved"] is False


def test_get_history_returns_only_user_visible_messages():
    with patch.object(server, "get_api_key", return_value="fake-key"), \
         patch.object(server, "OpenRouterProvider"), \
         patch.object(server, "Store") as MockStore:
        _configure(MockStore, history=[
            {"role": "user", "content": "read x"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "file body"},
            {"role": "assistant", "content": "it says hi"},
            {"role": "user", "content": [{"type": "image_url"}]},  # non-string content
            {"role": "assistant", "content": "   "},
        ])
        client = TestClient(server.app)
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "get_history"})
            msg = ws.receive_json()
    assert msg == {"type": "history", "session": 1, "items": [
        {"role": "user", "text": "read x"}, {"role": "assistant", "text": "it says hi"},
    ]}


def test_history_items_is_capped_to_the_most_recent():
    history = [{"role": "user", "content": f"m{i}"} for i in range(server.MAX_HISTORY_ITEMS + 25)]
    items = server.history_items(history)
    assert len(items) == server.MAX_HISTORY_ITEMS
    assert items[-1]["text"] == f"m{server.MAX_HISTORY_ITEMS + 24}"


# ---------------------------------------------------------------------------
# Chat sessions -- these use a REAL Store on a temp database (no mocks), since
# the behaviour under test is the interaction between server and store.
# ---------------------------------------------------------------------------

@pytest.fixture
def real_store(tmp_path):
    from raven.store import Store as RealStore
    db = tmp_path / "h.db"
    with patch.object(server, "get_api_key", return_value="fake-key"), \
         patch.object(server, "OpenRouterProvider") as MockProvider, \
         patch.object(server, "Store", side_effect=lambda **kw: RealStore(db, **kw)):
        MockProvider.return_value.reply.return_value = {"role": "assistant", "content": "ok"}
        yield RealStore(db), MockProvider


def _ask(ws, text):
    ws.send_json({"type": "ask", "text": text})
    reply = _recv(ws, "reply")
    sessions = _recv(ws, "sessions")
    # _run_ask also sends a "stats" message after "sessions" -- drain it too,
    # so it can never be mistaken for the next thing this connection sends
    # (found live: it was, shifting every later ws.receive_json() in a test
    # that calls _ask() more than once).
    _recv(ws, "stats")
    return reply, sessions


def test_a_reply_is_followed_by_the_updated_session_list_with_an_auto_title(real_store):
    with TestClient(server.app).websocket_connect("/ws") as ws:
        reply, sessions = _ask(ws, "Summarize the quarterly report")
    assert reply == {"type": "reply", "text": "ok"}
    assert sessions["type"] == "sessions"
    assert [x["title"] for x in sessions["items"]] == ["Summarize the quarterly report"]
    assert sessions["current"] == sessions["items"][0]["id"]


def test_new_session_starts_an_empty_chat_and_lists_both(real_store):
    with TestClient(server.app).websocket_connect("/ws") as ws:
        _ask(ws, "first chat")
        ws.send_json({"type": "new_session"})
        # new_session's own _send_history() also sends a "stats" message
        # between "history" and "sessions" -- see _ask()'s own comment.
        history, sessions = _recv(ws, "history"), _recv(ws, "sessions")
    assert history["type"] == "history" and history["items"] == []
    assert len(sessions["items"]) == 2 and sessions["current"] == history["session"]
    assert sessions["items"][0]["title"] == "New chat"   # the empty new one is most recent... 
    assert "first chat" in [x["title"] for x in sessions["items"]]


def test_new_session_while_already_empty_does_not_pile_up_empty_chats(real_store):
    with TestClient(server.app).websocket_connect("/ws") as ws:
        ws.send_json({"type": "new_session"}); _recv(ws, "history"); first = _recv(ws, "sessions")
        ws.send_json({"type": "new_session"}); _recv(ws, "history"); second = _recv(ws, "sessions")
    assert len(first["items"]) == len(second["items"]) == 1


def test_open_session_switches_history_and_keeps_chats_separate(real_store):
    with TestClient(server.app).websocket_connect("/ws") as ws:
        _, s1 = _ask(ws, "alpha topic")
        alpha = s1["current"]
        ws.send_json({"type": "new_session"}); _recv(ws, "history"); _recv(ws, "sessions")
        _ask(ws, "beta topic")
        ws.send_json({"type": "open_session", "id": alpha})
        history, sessions = _recv(ws, "history"), _recv(ws, "sessions")
    assert [i["text"] for i in history["items"]] == ["alpha topic", "ok"]
    assert sessions["current"] == alpha


def test_the_assistant_after_switching_only_sees_that_chats_history(real_store):
    store, MockProvider = real_store
    with TestClient(server.app).websocket_connect("/ws") as ws:
        _, s1 = _ask(ws, "alpha topic")
        ws.send_json({"type": "new_session"}); _recv(ws, "history"); _recv(ws, "sessions")
        _ask(ws, "beta topic")
    sent = [m.get("content") for m in MockProvider.return_value.reply.call_args[0][0]]
    assert "beta topic" in sent and "alpha topic" not in sent


def test_delete_session_removes_it_and_falls_back_to_another(real_store):
    with TestClient(server.app).websocket_connect("/ws") as ws:
        _, s1 = _ask(ws, "alpha topic")
        alpha = s1["current"]
        ws.send_json({"type": "new_session"}); _recv(ws, "history"); _recv(ws, "sessions")
        _, s2 = _ask(ws, "beta topic")
        beta = s2["current"]
        ws.send_json({"type": "delete_session", "id": beta})
        history, sessions = _recv(ws, "history"), _recv(ws, "sessions")
    assert [x["id"] for x in sessions["items"]] == [alpha]
    assert sessions["current"] == alpha and [i["text"] for i in history["items"]] == ["alpha topic", "ok"]


def test_deleting_the_only_session_leaves_a_fresh_empty_one(real_store):
    with TestClient(server.app).websocket_connect("/ws") as ws:
        _, s1 = _ask(ws, "only chat")
        ws.send_json({"type": "delete_session", "id": s1["current"]})
        history, sessions = _recv(ws, "history"), _recv(ws, "sessions")
    assert history["items"] == [] and len(sessions["items"]) == 1 and sessions["items"][0]["title"] == "New chat"


def test_opening_an_unknown_session_reports_an_error(real_store):
    with TestClient(server.app).websocket_connect("/ws") as ws:
        ws.send_json({"type": "open_session", "id": 9999})
        msg = ws.receive_json()
    assert msg["type"] == "error" and "no longer exists" in msg["message"]


def test_a_new_connection_reopens_the_most_recent_chat(real_store):
    client = TestClient(server.app)
    with client.websocket_connect("/ws") as ws:
        _, s1 = _ask(ws, "remember me")
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "get_history"})
        history = ws.receive_json()
    assert history["session"] == s1["current"]
    assert [i["text"] for i in history["items"]] == ["remember me", "ok"]


# ---------------------------------------------------------------------------
# Voice input (push-to-talk from the desktop UI): "transcribe" -> "transcript"
# / "transcript_error". Runs independently of Store/Assistant, so a minimal
# connection (no ask/session traffic) is enough -- only raven.server.voice is
# mocked, never the real faster-whisper/sounddevice stack.
# ---------------------------------------------------------------------------

def _connect():
    with patch.object(server, "get_api_key", return_value="fake-key"), \
         patch.object(server, "OpenRouterProvider"), \
         patch.object(server, "Store") as MockStore:
        _configure(MockStore)
        client = TestClient(server.app)
        with client.websocket_connect("/ws") as ws:
            yield ws


def test_transcribe_reports_when_voice_dependencies_are_missing():
    with patch.object(server.voice, "is_available", return_value=False):
        for ws in _connect():
            ws.send_json({"type": "transcribe", "audio": "", "id": "r1"})
            msg = _recv(ws, "transcript_error")
    assert msg == {"type": "transcript_error", "id": "r1",
                   "message": 'Voice dependencies aren\'t installed -- pip install ".[voice]" '
                              "(and on Linux: sudo apt install libportaudio2)."}


def test_transcribe_reports_when_the_model_is_not_downloaded():
    with patch.object(server.voice, "is_available", return_value=True), \
         patch.object(server.voice, "model_ready", return_value=False):
        for ws in _connect():
            ws.send_json({"type": "transcribe", "audio": "", "id": "r1"})
            msg = _recv(ws, "transcript_error")
    assert msg == {"type": "transcript_error", "id": "r1",
                   "message": "Voice model not downloaded -- run: python -m raven.voice"}


def test_transcribe_decodes_16bit_pcm_and_returns_the_transcript():
    import base64
    import numpy as np
    # A tiny, deliberately exact sample -- half and negative-half full scale --
    # so the int16 -> float32 conversion is checked precisely, not just "some array".
    pcm = np.array([16384, -16384, 0], dtype="<i2")
    audio_b64 = base64.b64encode(pcm.tobytes()).decode()
    seen = {}

    def fake_transcribe(samples, model_name=None):
        seen["samples"] = samples.copy()
        return "hello there"

    with patch.object(server.voice, "is_available", return_value=True), \
         patch.object(server.voice, "model_ready", return_value=True), \
         patch.object(server.voice, "transcribe", side_effect=fake_transcribe):
        for ws in _connect():
            ws.send_json({"type": "transcribe", "audio": audio_b64, "id": "r2"})
            msg = _recv(ws, "transcript")
    assert msg == {"type": "transcript", "id": "r2", "text": "hello there"}
    assert seen["samples"] == pytest.approx([0.5, -0.5, 0.0], abs=1e-3)


def test_transcribe_surfaces_a_missing_model_raised_from_transcribe_itself():
    """model_ready() can race with a concurrent download/deletion -- transcribe()
    itself raising ModelNotDownloadedError must be caught with the same clear
    message, not just the pre-flight model_ready() check."""
    from raven.voice import ModelNotDownloadedError
    with patch.object(server.voice, "is_available", return_value=True), \
         patch.object(server.voice, "model_ready", return_value=True), \
         patch.object(server.voice, "transcribe", side_effect=ModelNotDownloadedError("go download it")):
        for ws in _connect():
            ws.send_json({"type": "transcribe", "audio": "", "id": "r3"})
            msg = _recv(ws, "transcript_error")
    assert msg == {"type": "transcript_error", "id": "r3", "message": "go download it"}


def test_transcribe_reports_an_unexpected_failure_without_crashing_the_connection():
    with patch.object(server.voice, "is_available", return_value=True), \
         patch.object(server.voice, "model_ready", return_value=True), \
         patch.object(server.voice, "transcribe", side_effect=RuntimeError("boom")):
        for ws in _connect():
            ws.send_json({"type": "transcribe", "audio": "", "id": "r4"})
            msg = _recv(ws, "transcript_error")
    assert msg["type"] == "transcript_error" and "boom" in msg["message"]


def test_transcribe_can_run_while_an_ask_is_in_flight():
    """Unlike new_session/open_session/delete_session, transcribe must NOT be
    blocked by the 'a request is already running' guard -- it never touches
    self.assistant or self.store."""
    with patch.object(server, "get_api_key", return_value="fake-key"), \
         patch.object(server, "OpenRouterProvider") as MockProvider, \
         patch.object(server, "Store") as MockStore, \
         patch.object(server.voice, "is_available", return_value=True), \
         patch.object(server.voice, "model_ready", return_value=True), \
         patch.object(server.voice, "transcribe", return_value="a said thing"):
        _configure(MockStore)
        MockProvider.return_value.reply.return_value = {"role": "assistant", "content": "ok"}
        with TestClient(server.app).websocket_connect("/ws") as ws:
            ws.send_json({"type": "ask", "text": "slow request"})
            ws.send_json({"type": "transcribe", "audio": "", "id": "r5"})
            # ask() also triggers "sessions"/"stats" follow-ups, and the background
            # statusline push (Session.start_stats_loop) can interleave further
            # unrelated messages at any point on a live connection -- read
            # generously until both operations are seen, not a fixed count.
            wanted = {"reply", "transcript"}
            kinds = set()
            for _ in range(20):
                kinds.add(ws.receive_json()["type"])
                if wanted <= kinds:
                    break
    assert wanted <= kinds



# ---------------------------------------------------------------------------
# Model provider/name (desktop Settings panel): get_model_config, list_models,
# set_model. load_settings/save_settings are patched wherever a test needs
# control over -- or must never touch -- the real settings.json. Connections
# are opened directly (not via the transcribe tests' _connect() helper, whose
# own internal patches would shadow a test's own). Replies are drained with
# _recv(..., msg_type) rather than assumed to be the very next message,
# because the background statusline push (Session.start_stats_loop) can
# interleave a "stats" message at any point on a live connection.
# ---------------------------------------------------------------------------

def _default_model_settings():
    return {
        "model": {"provider": "openrouter", "name": "openrouter/free", "fallbacks": ["a", "b"], "vision": "v"},
        "statusline": {"segments": ["memory"]},  # cheap, provider-independent -- see module docstring above
    }


def _recv(ws, msg_type, max_tries=8):
    """Read messages until one of type `msg_type` arrives (skipping any
    interleaved background "stats" pushes), or fail after max_tries."""
    for _ in range(max_tries):
        msg = ws.receive_json()
        if msg["type"] == msg_type:
            return msg
    raise AssertionError(f"no {msg_type!r} message received in {max_tries} tries")


def test_get_model_config_reports_the_current_provider_and_name():
    with patch.object(server, "get_api_key", return_value="fake-key"), \
         patch.object(server, "OpenRouterProvider"), \
         patch.object(server, "Store") as MockStore, \
         patch.object(server, "load_settings", return_value=_default_model_settings()):
        _configure(MockStore)
        with TestClient(server.app).websocket_connect("/ws") as ws:
            ws.send_json({"type": "get_model_config"})
            msg = _recv(ws, "model_config")
    assert msg == {"type": "model_config", "provider": "openrouter", "name": "openrouter/free"}


def test_list_models_returns_the_nvidia_catalog():
    with patch.object(server, "get_api_key", return_value="fake-key") as mock_get_key, \
         patch.object(server, "OpenRouterProvider"), \
         patch.object(server, "Store") as MockStore, \
         patch.object(server, "list_nvidia_models", return_value=["meta/llama-3.1-8b-instruct", "google/gemma-2-9b"]) as mock_list:
        _configure(MockStore)
        with TestClient(server.app).websocket_connect("/ws") as ws:
            ws.send_json({"type": "list_models", "provider": "nvidia"})
            msg = _recv(ws, "models")
    mock_get_key.assert_any_call("nvidia", ANY)
    mock_list.assert_called_once_with("fake-key")
    assert msg == {"type": "models", "provider": "nvidia", "items": ["meta/llama-3.1-8b-instruct", "google/gemma-2-9b"]}


def test_list_models_reports_a_failure_without_crashing_the_connection():
    with patch.object(server, "get_api_key", return_value="fake-key"), \
         patch.object(server, "OpenRouterProvider"), \
         patch.object(server, "Store") as MockStore, \
         patch.object(server, "list_nvidia_models", side_effect=RuntimeError("NVIDIA 401: bad key")):
        _configure(MockStore)
        with TestClient(server.app).websocket_connect("/ws") as ws:
            ws.send_json({"type": "list_models", "provider": "nvidia"})
            msg = _recv(ws, "models_error")
    assert msg == {"type": "models_error", "provider": "nvidia", "message": "NVIDIA 401: bad key"}


def test_list_models_for_an_unsupported_provider_is_a_clear_error_not_a_network_call():
    with patch.object(server, "get_api_key", return_value="fake-key"), \
         patch.object(server, "OpenRouterProvider"), \
         patch.object(server, "Store") as MockStore, \
         patch.object(server, "list_nvidia_models") as mock_list:
        _configure(MockStore)
        with TestClient(server.app).websocket_connect("/ws") as ws:
            ws.send_json({"type": "list_models", "provider": "openrouter"})
            msg = _recv(ws, "models_error")
    mock_list.assert_not_called()
    assert msg["provider"] == "openrouter"


def test_set_model_switches_provider_persists_and_takes_effect_immediately():
    settings = _default_model_settings()
    with patch.object(server, "get_api_key", return_value="nv-key") as mock_get_key, \
         patch.object(server, "OpenRouterProvider"), \
         patch.object(server, "NvidiaProvider") as MockNvidia, \
         patch.object(server, "Store") as MockStore, \
         patch.object(server, "load_settings", return_value=settings), \
         patch.object(server, "save_settings") as mock_save:
        _configure(MockStore)
        with TestClient(server.app).websocket_connect("/ws") as ws:
            ws.send_json({"type": "set_model", "provider": "nvidia", "name": "meta/llama-3.1-8b-instruct"})
            config_msg = _recv(ws, "model_config")

    mock_get_key.assert_any_call("nvidia", ANY)
    # switching PROVIDER (openrouter -> nvidia) must drop the old provider's fallbacks
    MockNvidia.assert_called_once_with(api_key="nv-key", model="meta/llama-3.1-8b-instruct", fallbacks=[])
    saved = mock_save.call_args[0][0]
    assert saved["model"]["provider"] == "nvidia"
    assert saved["model"]["name"] == "meta/llama-3.1-8b-instruct"
    assert saved["model"]["fallbacks"] == []
    assert saved["model"]["vision"] == "v"  # untouched
    assert config_msg == {"type": "model_config", "provider": "nvidia", "name": "meta/llama-3.1-8b-instruct"}


def test_set_model_same_provider_different_model_keeps_fallbacks():
    settings = _default_model_settings()
    with patch.object(server, "get_api_key", return_value="fake-key"), \
         patch.object(server, "OpenRouterProvider") as MockOpenRouter, \
         patch.object(server, "Store") as MockStore, \
         patch.object(server, "load_settings", return_value=settings), \
         patch.object(server, "save_settings") as mock_save:
        _configure(MockStore)
        with TestClient(server.app).websocket_connect("/ws") as ws:
            ws.send_json({"type": "set_model", "provider": "openrouter", "name": "some/other-model:free"})
            _recv(ws, "model_config")

    saved = mock_save.call_args[0][0]
    assert saved["model"]["fallbacks"] == ["a", "b"]
    # the LAST call is the live switch, made with the SAME (unchanged) fallbacks
    assert MockOpenRouter.call_args.kwargs["fallbacks"] == ["a", "b"]


def test_set_model_rejects_an_unknown_provider_without_touching_settings():
    with patch.object(server, "get_api_key", return_value="fake-key"), \
         patch.object(server, "OpenRouterProvider"), \
         patch.object(server, "Store") as MockStore, \
         patch.object(server, "save_settings") as mock_save:
        _configure(MockStore)
        with TestClient(server.app).websocket_connect("/ws") as ws:
            ws.send_json({"type": "set_model", "provider": "openai", "name": "gpt-4"})
            msg = _recv(ws, "error")
    assert "Unknown model provider" in msg["message"]
    mock_save.assert_not_called()


def test_set_model_rejects_an_empty_name():
    with patch.object(server, "get_api_key", return_value="fake-key"), \
         patch.object(server, "OpenRouterProvider"), \
         patch.object(server, "Store") as MockStore, \
         patch.object(server, "save_settings") as mock_save:
        _configure(MockStore)
        with TestClient(server.app).websocket_connect("/ws") as ws:
            ws.send_json({"type": "set_model", "provider": "nvidia", "name": "  "})
            msg = _recv(ws, "error")
    assert "model name is required" in msg["message"]
    mock_save.assert_not_called()


def test_set_model_a_bad_api_key_leaves_the_current_model_untouched():
    """Validated by actually constructing the new provider BEFORE persisting
    or swapping self.assistant.provider -- a missing/bad key must not silently
    strand the session on a broken model or corrupt settings.json."""
    settings = _default_model_settings()

    def flaky_get_key(provider, settings=None):
        if provider == "nvidia":
            raise RuntimeError("Set NVIDIA_API_KEY in your environment or .env file")
        return "fake-key"

    with patch.object(server, "get_api_key", side_effect=flaky_get_key), \
         patch.object(server, "OpenRouterProvider"), \
         patch.object(server, "Store") as MockStore, \
         patch.object(server, "load_settings", return_value=settings), \
         patch.object(server, "save_settings") as mock_save:
        _configure(MockStore)
        with TestClient(server.app).websocket_connect("/ws") as ws:
            ws.send_json({"type": "set_model", "provider": "nvidia", "name": "meta/llama-3.1-8b-instruct"})
            msg = _recv(ws, "error")
    assert "NVIDIA_API_KEY" in msg["message"]
    mock_save.assert_not_called()


# ---------------------------------------------------------------------------
# user_config / set_api_key / set_user_name (desktop Settings panel release
# goal): a fresh install configured entirely from the UI -- no .env file,
# no hand-edited settings.json. The key itself is never echoed back.
# ---------------------------------------------------------------------------

def _default_user_settings():
    return {
        "model": {"provider": "openrouter", "name": "openrouter/free", "fallbacks": []},
        "api_keys": {"openrouter": "", "nvidia": "", "freellmapi": ""},
        "user": {"name": ""},
        "statusline": {"segments": ["memory"]},
    }


def test_get_user_config_reports_name_and_which_providers_have_a_key():
    settings = _default_user_settings()
    settings["user"]["name"] = "Ely"
    settings["api_keys"]["nvidia"] = "nv-key"
    with patch.object(server, "get_api_key", return_value="fake-key"), \
         patch.object(server, "OpenRouterProvider"), \
         patch.object(server, "Store") as MockStore, \
         patch.object(server, "load_settings", return_value=settings):
        _configure(MockStore)
        with TestClient(server.app).websocket_connect("/ws") as ws:
            ws.send_json({"type": "get_user_config"})
            msg = _recv(ws, "user_config")
    assert msg == {
        "type": "user_config", "name": "Ely",
        "has_key": {"openrouter": False, "nvidia": True, "freellmapi": False},
    }


def test_set_api_key_persists_and_never_echoes_the_key_back():
    settings = _default_user_settings()
    with patch.object(server, "get_api_key", return_value="fake-key"), \
         patch.object(server, "OpenRouterProvider") as MockOpenRouter, \
         patch.object(server, "Store") as MockStore, \
         patch.object(server, "load_settings", return_value=settings), \
         patch.object(server, "save_settings") as mock_save:
        _configure(MockStore)
        with TestClient(server.app).websocket_connect("/ws") as ws:
            ws.send_json({"type": "set_api_key", "provider": "openrouter", "key": "sk-secret"})
            msg = _recv(ws, "user_config")
    saved = mock_save.call_args[0][0]
    assert saved["api_keys"]["openrouter"] == "sk-secret"
    assert "sk-secret" not in json.dumps(msg)
    # it's the active provider -- takes effect immediately, same as set_model
    MockOpenRouter.assert_called_with(api_key="sk-secret", model="openrouter/free", fallbacks=[])


def test_set_api_key_for_a_different_provider_persists_without_rebuilding_the_live_provider():
    settings = _default_user_settings()  # active provider is openrouter
    with patch.object(server, "get_api_key", return_value="fake-key"), \
         patch.object(server, "OpenRouterProvider") as MockOpenRouter, \
         patch.object(server, "NvidiaProvider") as MockNvidia, \
         patch.object(server, "Store") as MockStore, \
         patch.object(server, "load_settings", return_value=settings), \
         patch.object(server, "save_settings") as mock_save:
        _configure(MockStore)
        with TestClient(server.app).websocket_connect("/ws") as ws:
            ws.send_json({"type": "set_api_key", "provider": "nvidia", "key": "nv-secret"})
            _recv(ws, "user_config")
    assert mock_save.call_args[0][0]["api_keys"]["nvidia"] == "nv-secret"
    MockNvidia.assert_not_called()


def test_set_api_key_rejects_an_unknown_provider():
    with patch.object(server, "get_api_key", return_value="fake-key"), \
         patch.object(server, "OpenRouterProvider"), \
         patch.object(server, "Store") as MockStore, \
         patch.object(server, "save_settings") as mock_save:
        _configure(MockStore)
        with TestClient(server.app).websocket_connect("/ws") as ws:
            ws.send_json({"type": "set_api_key", "provider": "openai", "key": "x"})
            msg = _recv(ws, "error")
    assert "Unknown model provider" in msg["message"]
    mock_save.assert_not_called()


def test_set_api_key_rejects_an_empty_key():
    with patch.object(server, "get_api_key", return_value="fake-key"), \
         patch.object(server, "OpenRouterProvider"), \
         patch.object(server, "Store") as MockStore, \
         patch.object(server, "save_settings") as mock_save:
        _configure(MockStore)
        with TestClient(server.app).websocket_connect("/ws") as ws:
            ws.send_json({"type": "set_api_key", "provider": "openrouter", "key": "   "})
            msg = _recv(ws, "error")
    assert "API key is required" in msg["message"]
    mock_save.assert_not_called()


def test_set_user_name_persists_and_is_reported_back():
    settings = _default_user_settings()
    with patch.object(server, "get_api_key", return_value="fake-key"), \
         patch.object(server, "OpenRouterProvider"), \
         patch.object(server, "Store") as MockStore, \
         patch.object(server, "load_settings", return_value=settings), \
         patch.object(server, "save_settings") as mock_save:
        _configure(MockStore)
        with TestClient(server.app).websocket_connect("/ws") as ws:
            ws.send_json({"type": "set_user_name", "name": "Ely"})
            msg = _recv(ws, "user_config")
    assert mock_save.call_args[0][0]["user"]["name"] == "Ely"
    assert msg["name"] == "Ely"


# ---------------------------------------------------------------------------
# Cancelling an in-flight "ask" (desktop UI): cancel_ask -> cancel_requested
# (immediate ack) -> "cancelled" (once the underlying, uninterruptible call
# actually finishes). A real Store is used so the history-rollback can be
# checked for real, not just inferred from a mock.
# ---------------------------------------------------------------------------

def test_cancel_ask_with_nothing_in_flight_is_a_clear_error():
    with patch.object(server, "get_api_key", return_value="fake-key"), \
         patch.object(server, "OpenRouterProvider"), \
         patch.object(server, "Store") as MockStore:
        _configure(MockStore)
        with TestClient(server.app).websocket_connect("/ws") as ws:
            ws.send_json({"type": "cancel_ask"})
            msg = _recv(ws, "error")
    assert "Nothing to cancel" in msg["message"]


def test_cancel_ask_acknowledges_immediately_then_reports_cancelled_when_settled(real_store):
    store, MockProvider = real_store
    started = threading.Event()
    release = threading.Event()

    def slow_reply(messages, tools=None):
        started.set()
        release.wait(timeout=5)
        return {"role": "assistant", "content": "too late"}

    MockProvider.return_value.reply.side_effect = slow_reply
    with TestClient(server.app).websocket_connect("/ws") as ws:
        ws.send_json({"type": "ask", "text": "slow one"})
        assert started.wait(timeout=5)  # the call is genuinely in flight now
        ws.send_json({"type": "cancel_ask"})
        ack = _recv(ws, "cancel_requested")
        release.set()  # let the "uninterruptible" call finish
        settled = _recv(ws, "cancelled")
    assert ack == {"type": "cancel_requested"}
    assert settled == {"type": "cancelled"}
    # Known, documented limitation (see _run_ask's own comment): Assistant.ask()
    # already persisted this turn to the store before _run_ask learned it was
    # cancelled, so it's genuinely still on disk. The live conversation
    # (self.assistant.history, what the model/UI actually see for the rest of
    # this session) is what must be clean -- checked more strongly by
    # test_cancel_ask_rolls_back_the_turn_from_live_history below.
    assert any(m.get("content") == "too late" for m in store.load())


def test_cancel_ask_rolls_back_the_turn_from_live_history(real_store):
    """Stronger check than the message-level one above: the exact user
    message and reply from the cancelled turn must be gone from
    self.assistant.history (what get_history/the model both read), not just
    absent from the "cancelled" response."""
    store, MockProvider = real_store
    release = threading.Event()
    MockProvider.return_value.reply.side_effect = lambda messages, tools=None: (
        release.wait(timeout=5),
        {"role": "assistant", "content": "should be discarded"},
    )[1]

    with TestClient(server.app).websocket_connect("/ws") as ws:
        ws.send_json({"type": "ask", "text": "please cancel me"})
        ws.send_json({"type": "cancel_ask"})
        _recv(ws, "cancel_requested")
        release.set()
        _recv(ws, "cancelled")

        ws.send_json({"type": "get_history"})
        history = _recv(ws, "history")
    texts = [i["text"] for i in history["items"]]
    assert "please cancel me" not in texts
    assert "should be discarded" not in texts


def test_a_new_ask_is_refused_while_one_is_in_flight(real_store):
    store, MockProvider = real_store
    release = threading.Event()
    MockProvider.return_value.reply.side_effect = lambda messages, tools=None: (
        release.wait(timeout=5),
        {"role": "assistant", "content": "first"},
    )[1]

    with TestClient(server.app).websocket_connect("/ws") as ws:
        ws.send_json({"type": "ask", "text": "first one"})
        ws.send_json({"type": "ask", "text": "second one"})
        msg = _recv(ws, "error")
        release.set()
        _recv(ws, "reply")
    assert "Wait for the current request" in msg["message"]


def test_a_new_ask_is_allowed_immediately_after_a_cancelled_one_settles(real_store):
    """The refusal above must not become permanent -- once a cancelled ask's
    background call actually finishes, a fresh one must go through normally."""
    store, MockProvider = real_store
    release = threading.Event()
    calls = []

    def flaky(messages, tools=None):
        calls.append(1)
        if len(calls) == 1:
            release.wait(timeout=5)
            return {"role": "assistant", "content": "first (discarded)"}
        return {"role": "assistant", "content": "second, real reply"}

    MockProvider.return_value.reply.side_effect = flaky

    with TestClient(server.app).websocket_connect("/ws") as ws:
        ws.send_json({"type": "ask", "text": "first"})
        ws.send_json({"type": "cancel_ask"})
        _recv(ws, "cancel_requested")
        release.set()
        _recv(ws, "cancelled")

        ws.send_json({"type": "ask", "text": "second"})
        reply = _recv(ws, "reply")
    assert reply == {"type": "reply", "text": "second, real reply"}


def test_cancelling_a_call_that_then_fails_reports_cancelled_not_an_error(real_store):
    """A cancelled call that ALSO errors out (e.g. the network request fails
    after the user gave up on it) must still read as a cancellation to the
    user, not a scary unrelated error."""
    store, MockProvider = real_store
    release = threading.Event()

    def failing(messages, tools=None):
        release.wait(timeout=5)
        raise RuntimeError("network blew up after the user stopped waiting")

    MockProvider.return_value.reply.side_effect = failing

    with TestClient(server.app).websocket_connect("/ws") as ws:
        ws.send_json({"type": "ask", "text": "doomed"})
        ws.send_json({"type": "cancel_ask"})
        _recv(ws, "cancel_requested")
        release.set()
        settled = _recv(ws, "cancelled")
    assert settled == {"type": "cancelled"}


# ---------------------------------------------------------------------------
# Design mode: the canvas protocol. Uses the same real_store fixture as
# sessions above -- the behavior under test is the interaction between
# server, Assistant's canvas, and the real SQLite store.
# ---------------------------------------------------------------------------

def _draw_via_tool_call(MockProvider, ws, name="draw_polygon", args=None):
    """Scripts the mocked provider to emit one tool call then a final reply,
    drives one ask() through it, and returns the pushed canvas_update
    element. Mirrors _ask()'s draining discipline for the extra messages a
    tool-calling turn produces (tool_call, canvas_update, reply, sessions,
    stats)."""
    args = args or {"sides": 6, "radius": 80}
    MockProvider.return_value.reply.side_effect = [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "function": {"name": name, "arguments": json.dumps(args)}}]},
        {"role": "assistant", "content": "done"},
    ]
    ws.send_json({"type": "ask", "text": "draw something"})
    update = _recv(ws, "canvas_update")
    _recv(ws, "reply")
    _recv(ws, "sessions")
    _recv(ws, "stats")
    return update["element"]


def test_canvas_is_sent_alongside_history_on_connect():
    with patch.object(server, "get_api_key", return_value="fake-key"), \
         patch.object(server, "OpenRouterProvider"), \
         patch.object(server, "Store") as MockStore:
        _configure(MockStore)
        with TestClient(server.app).websocket_connect("/ws") as ws:
            ws.send_json({"type": "get_history"})
            _recv(ws, "history")
            canvas = _recv(ws, "canvas")
    assert canvas == {"type": "canvas", "session": 1, "elements": []}


def test_get_canvas_message_returns_the_current_canvas(real_store):
    with TestClient(server.app).websocket_connect("/ws") as ws:
        ws.send_json({"type": "get_canvas"})
        canvas = _recv(ws, "canvas")
    assert canvas["elements"] == []


def test_a_draw_tool_call_pushes_a_canvas_update(real_store):
    store, MockProvider = real_store
    with TestClient(server.app).websocket_connect("/ws") as ws:
        element = _draw_via_tool_call(MockProvider, ws)
    assert element["type"] == "polygon"
    assert element["params"]["sides"] == 6


def test_a_drawn_shape_persists_and_reloads_on_reconnect(real_store):
    store, MockProvider = real_store
    with TestClient(server.app).websocket_connect("/ws") as ws:
        element = _draw_via_tool_call(MockProvider, ws)
    with TestClient(server.app).websocket_connect("/ws") as ws2:
        ws2.send_json({"type": "get_history"})
        _recv(ws2, "history")
        canvas = _recv(ws2, "canvas")
    assert [el["id"] for el in canvas["elements"]] == [element["id"]]


def test_canvas_edit_updates_the_element_and_pushes_the_result(real_store):
    store, MockProvider = real_store
    with TestClient(server.app).websocket_connect("/ws") as ws:
        element = _draw_via_tool_call(MockProvider, ws)
        ws.send_json({"type": "canvas_edit", "element_id": element["id"], "patch": {"x": 250}})
        update = _recv(ws, "canvas_update")
    assert update["element"]["x"] == 250
    assert update["element"]["id"] == element["id"]


def test_canvas_edit_on_an_unknown_id_reports_a_clear_error(real_store):
    with TestClient(server.app).websocket_connect("/ws") as ws:
        ws.send_json({"type": "canvas_edit", "element_id": "nope", "patch": {"x": 1}})
        error = _recv(ws, "error")
    assert "No such canvas element" in error["message"]


def test_canvas_delete_message_removes_the_element(real_store):
    store, MockProvider = real_store
    with TestClient(server.app).websocket_connect("/ws") as ws:
        element = _draw_via_tool_call(MockProvider, ws)
        ws.send_json({"type": "canvas_delete", "element_id": element["id"]})
        deleted = _recv(ws, "canvas_delete")
    assert deleted["element_id"] == element["id"]


def test_canvas_delete_on_an_unknown_id_reports_a_clear_error(real_store):
    with TestClient(server.app).websocket_connect("/ws") as ws:
        ws.send_json({"type": "canvas_delete", "element_id": "nope"})
        error = _recv(ws, "error")
    assert "No such canvas element" in error["message"]


def test_canvas_group_and_ungroup_round_trip(real_store):
    store, MockProvider = real_store
    with TestClient(server.app).websocket_connect("/ws") as ws:
        first = _draw_via_tool_call(MockProvider, ws, args={"sides": 3, "radius": 10})
        second = _draw_via_tool_call(MockProvider, ws, name="draw_star",
                                      args={"points": 5, "outer_radius": 10, "inner_radius": 4})
        ws.send_json({"type": "canvas_group", "element_ids": [first["id"], second["id"]]})
        grouped = _recv(ws, "canvas_update")
        assert grouped["element"]["type"] == "group"
        group_id = grouped["element"]["id"]

        ws.send_json({"type": "canvas_ungroup", "group_id": group_id})
        deleted = _recv(ws, "canvas_delete")
        assert deleted["element_id"] == group_id


def test_canvas_group_with_fewer_than_two_elements_reports_a_clear_error(real_store):
    store, MockProvider = real_store
    with TestClient(server.app).websocket_connect("/ws") as ws:
        element = _draw_via_tool_call(MockProvider, ws)
        ws.send_json({"type": "canvas_group", "element_ids": [element["id"]]})
        error = _recv(ws, "error")
    assert "at least 2" in error["message"]


def test_canvas_ungroup_on_an_unknown_id_reports_a_clear_error(real_store):
    with TestClient(server.app).websocket_connect("/ws") as ws:
        ws.send_json({"type": "canvas_ungroup", "group_id": "nope"})
        error = _recv(ws, "error")
    assert "No such group" in error["message"]


def test_switching_sessions_switches_the_canvas_too(real_store):
    store, MockProvider = real_store
    with TestClient(server.app).websocket_connect("/ws") as ws:
        first_element = _draw_via_tool_call(MockProvider, ws)
        ws.send_json({"type": "new_session"})
        _recv(ws, "history")
        empty_canvas = _recv(ws, "canvas")
        _recv(ws, "sessions")
        assert empty_canvas["elements"] == []
        second_element = _draw_via_tool_call(MockProvider, ws, args={"sides": 5, "radius": 30})
        assert second_element["id"] != first_element["id"]


def test_canvas_restore_puts_a_deleted_element_back(real_store):
    store, MockProvider = real_store
    with TestClient(server.app).websocket_connect("/ws") as ws:
        element = _draw_via_tool_call(MockProvider, ws)
        ws.send_json({"type": "canvas_delete", "element_id": element["id"]})
        _recv(ws, "canvas_delete")
        ws.send_json({"type": "canvas_restore", "element": element})
        update = _recv(ws, "canvas_update")
    assert update["element"]["id"] == element["id"]


def test_canvas_restore_with_an_invalid_element_reports_a_clear_error(real_store):
    with TestClient(server.app).websocket_connect("/ws") as ws:
        ws.send_json({"type": "canvas_restore", "element": "not-a-dict"})
        error = _recv(ws, "error")
    assert "Invalid element" in error["message"]


def test_list_models_returns_the_freellmapi_catalog(monkeypatch):
    monkeypatch.delenv("FREELLMAPI_BASE_URL", raising=False)
    with patch.object(server, "get_api_key", return_value="fla-key") as mock_get_key, \
         patch.object(server, "OpenRouterProvider"), \
         patch.object(server, "Store") as MockStore, \
         patch.object(server, "list_freellmapi_models", return_value=["auto", "nvidia/some-model"]) as mock_list:
        _configure(MockStore)
        with TestClient(server.app).websocket_connect("/ws") as ws:
            ws.send_json({"type": "list_models", "provider": "freellmapi"})
            msg = _recv(ws, "models")
    mock_get_key.assert_any_call("freellmapi", ANY)
    mock_list.assert_called_once_with("fla-key", server.FREELLMAPI_DEFAULT_BASE_URL)
    assert msg == {"type": "models", "provider": "freellmapi", "items": ["auto", "nvidia/some-model"]}


def test_list_models_freellmapi_uses_the_env_var_base_url_when_set(monkeypatch):
    monkeypatch.setenv("FREELLMAPI_BASE_URL", "http://localhost:3002/v1")
    with patch.object(server, "get_api_key", return_value="fla-key"), \
         patch.object(server, "OpenRouterProvider"), \
         patch.object(server, "Store") as MockStore, \
         patch.object(server, "list_freellmapi_models", return_value=[]) as mock_list:
        _configure(MockStore)
        with TestClient(server.app).websocket_connect("/ws") as ws:
            ws.send_json({"type": "list_models", "provider": "freellmapi"})
            _recv(ws, "models")
    mock_list.assert_called_once_with("fla-key", "http://localhost:3002/v1")


def test_set_model_switches_to_freellmapi_persists_and_takes_effect_immediately(monkeypatch):
    monkeypatch.delenv("FREELLMAPI_BASE_URL", raising=False)
    settings = _default_model_settings()
    with patch.object(server, "get_api_key", return_value="fla-key") as mock_get_key, \
         patch.object(server, "OpenRouterProvider"), \
         patch.object(server, "FreeLLMAPIProvider") as MockFreeLLMAPI, \
         patch.object(server, "Store") as MockStore, \
         patch.object(server, "load_settings", return_value=settings), \
         patch.object(server, "save_settings") as mock_save:
        _configure(MockStore)
        with TestClient(server.app).websocket_connect("/ws") as ws:
            ws.send_json({"type": "set_model", "provider": "freellmapi", "name": "auto"})
            _recv(ws, "model_config")

    mock_get_key.assert_any_call("freellmapi", ANY)
    MockFreeLLMAPI.assert_called_once_with(
        api_key="fla-key", model="auto", fallbacks=[], base_url=server.FREELLMAPI_DEFAULT_BASE_URL,
    )
    saved = mock_save.call_args[0][0]
    assert saved["model"]["provider"] == "freellmapi"
    assert saved["model"]["name"] == "auto"


def test_provider_cls_and_kwargs_covers_all_three_providers(monkeypatch):
    monkeypatch.delenv("FREELLMAPI_BASE_URL", raising=False)
    assert server._provider_cls_and_kwargs("nvidia") == (server.NvidiaProvider, {})
    assert server._provider_cls_and_kwargs("openrouter") == (server.OpenRouterProvider, {})
    assert server._provider_cls_and_kwargs("anything-else") == (server.OpenRouterProvider, {})
    cls, kwargs = server._provider_cls_and_kwargs("freellmapi")
    assert cls is server.FreeLLMAPIProvider
    assert kwargs == {"base_url": server.FREELLMAPI_DEFAULT_BASE_URL}


def test_cancel_ask_auto_denies_an_already_pending_confirm(real_store):
    """Cancelling while a tool's confirm dialog is already showing must not
    leave the user stuck deciding it by hand -- cancel_ask itself resolves it
    (denied), unblocking the background thread without any confirm_response
    from the client at all."""
    store, MockProvider = real_store
    MockProvider.return_value.reply.side_effect = [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "function": {"name": "delete_file", "arguments": '{"path": "x.txt"}'}}]},
        {"role": "assistant", "content": "I did not delete the file since you cancelled."},
    ]
    with TestClient(server.app).websocket_connect("/ws") as ws:
        ws.send_json({"type": "ask", "text": "delete x.txt"})
        ws.receive_json()  # tool_call notification
        confirm_msg = ws.receive_json()
        assert confirm_msg["type"] == "confirm_request"
        ws.send_json({"type": "cancel_ask"})
        ack = _recv(ws, "cancel_requested")
        settled = _recv(ws, "cancelled")  # no confirm_response ever sent for it
    assert ack == {"type": "cancel_requested"}
    assert settled == {"type": "cancelled"}


def test_cancel_ask_auto_denies_a_confirm_requested_after_cancellation(real_store):
    """A tool needing confirmation requested AFTER cancel_ask has already
    been processed (the model's tool-call iteration was simply still in
    flight) must be auto-denied too -- no confirm_request should even reach
    the client, and the real tool function must never run. The release Event
    pins down the ordering precisely: reply() only returns the tool_calls
    message once cancel_requested (proof cancel_ask was fully handled) has
    already arrived, so the confirm attempt that follows is unambiguously
    "after cancellation", not a race."""
    store, MockProvider = real_store
    release = threading.Event()
    calls = []

    def fake_reply(messages, tools=None):
        calls.append(1)
        if len(calls) == 1:
            release.wait(timeout=5)
            return {"role": "assistant", "content": None,
                    "tool_calls": [{"id": "c1", "function": {"name": "delete_file", "arguments": '{"path": "x.txt"}'}}]}
        return {"role": "assistant", "content": "done anyway"}

    MockProvider.return_value.reply.side_effect = fake_reply
    deleted = []
    with patch("raven.tools.TOOL_FUNCTIONS", {**__import__("raven.tools", fromlist=["x"]).TOOL_FUNCTIONS,
                                              "delete_file": lambda path: deleted.append(path)}):
        with TestClient(server.app).websocket_connect("/ws") as ws:
            ws.send_json({"type": "ask", "text": "delete x.txt"})
            ws.send_json({"type": "cancel_ask"})
            ack = _recv(ws, "cancel_requested")
            release.set()
            settled = _recv(ws, "cancelled")
    assert ack == {"type": "cancel_requested"}
    assert settled == {"type": "cancelled"}
    assert deleted == []  # auto-denied -- the real tool function never ran
