"""raven/server.py — the WebSocket bridge wrapping Assistant for the
desktop UI. Every test mocks get_api_key/OpenRouterProvider/Store (same
discipline as test_cli.py's build_assistant tests) so nothing here ever
touches a real API key, makes a real network call, or writes to the real
~/.raven/history.db. FastAPI's TestClient drives the real ASGI app
in-process (a real WebSocket handshake and message loop, not a mock of the
server itself) -- only the Assistant's own dependencies are faked.
"""
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from raven import server


def _configure(MockStore, history=None):
    """A mocked Store that behaves like an empty single-session store."""
    m = MockStore.return_value
    m.load.return_value = history or []
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
    reply = ws.receive_json()
    sessions = ws.receive_json()
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
        history, sessions = ws.receive_json(), ws.receive_json()
    assert history["type"] == "history" and history["items"] == []
    assert len(sessions["items"]) == 2 and sessions["current"] == history["session"]
    assert sessions["items"][0]["title"] == "New chat"   # the empty new one is most recent... 
    assert "first chat" in [x["title"] for x in sessions["items"]]


def test_new_session_while_already_empty_does_not_pile_up_empty_chats(real_store):
    with TestClient(server.app).websocket_connect("/ws") as ws:
        ws.send_json({"type": "new_session"}); ws.receive_json(); first = ws.receive_json()
        ws.send_json({"type": "new_session"}); ws.receive_json(); second = ws.receive_json()
    assert len(first["items"]) == len(second["items"]) == 1


def test_open_session_switches_history_and_keeps_chats_separate(real_store):
    with TestClient(server.app).websocket_connect("/ws") as ws:
        _, s1 = _ask(ws, "alpha topic")
        alpha = s1["current"]
        ws.send_json({"type": "new_session"}); ws.receive_json(); ws.receive_json()
        _ask(ws, "beta topic")
        ws.send_json({"type": "open_session", "id": alpha})
        history, sessions = ws.receive_json(), ws.receive_json()
    assert [i["text"] for i in history["items"]] == ["alpha topic", "ok"]
    assert sessions["current"] == alpha


def test_the_assistant_after_switching_only_sees_that_chats_history(real_store):
    store, MockProvider = real_store
    with TestClient(server.app).websocket_connect("/ws") as ws:
        _, s1 = _ask(ws, "alpha topic")
        ws.send_json({"type": "new_session"}); ws.receive_json(); ws.receive_json()
        _ask(ws, "beta topic")
    sent = [m.get("content") for m in MockProvider.return_value.reply.call_args[0][0]]
    assert "beta topic" in sent and "alpha topic" not in sent


def test_delete_session_removes_it_and_falls_back_to_another(real_store):
    with TestClient(server.app).websocket_connect("/ws") as ws:
        _, s1 = _ask(ws, "alpha topic")
        alpha = s1["current"]
        ws.send_json({"type": "new_session"}); ws.receive_json(); ws.receive_json()
        _, s2 = _ask(ws, "beta topic")
        beta = s2["current"]
        ws.send_json({"type": "delete_session", "id": beta})
        history, sessions = ws.receive_json(), ws.receive_json()
    assert [x["id"] for x in sessions["items"]] == [alpha]
    assert sessions["current"] == alpha and [i["text"] for i in history["items"]] == ["alpha topic", "ok"]


def test_deleting_the_only_session_leaves_a_fresh_empty_one(real_store):
    with TestClient(server.app).websocket_connect("/ws") as ws:
        _, s1 = _ask(ws, "only chat")
        ws.send_json({"type": "delete_session", "id": s1["current"]})
        history, sessions = ws.receive_json(), ws.receive_json()
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
