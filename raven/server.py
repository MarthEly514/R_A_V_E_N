"""HTTP/WebSocket API server wrapping Assistant, for the desktop UI
(raven_ui/, Electron+React) -- the "HTTP/API wrapper for other front-ends"
this project's own plan had listed as future Stage 5 work; the desktop app
is what finally justified building it.

Architecture, stated plainly since it's the one genuinely new piece: the
whole rest of R.A.V.E.N (Assistant, tools.py, llm_provider.py) is
synchronous Python, written with the CLI's blocking request/reply loop in
mind. This server runs Assistant.ask() in a background thread per request
(asyncio.to_thread), NOT on the asyncio event loop -- so the SAME
confirm_run/on_tool_call callables Assistant already calls synchronously
keep working unchanged; nothing in assistant.py or tools.py needed to
become async. Bridging a synchronous confirm_run call (which needs to ask
the browser-side UI a question and BLOCK for the answer) into the async
WebSocket world uses a threading.Event + asyncio.run_coroutine_threadsafe,
the same "confine blocking work to its own thread, cross the boundary
explicitly" shape browser.py already established for Playwright
(_run_in_browser_thread) -- applied here in the opposite direction (bridging
a sync call OUT to async code, rather than sync work off the main thread),
but the same underlying pattern, not a new one invented for this.

One Assistant instance per server process (matching the CLI's own
single-assistant assumption) -- not one per WebSocket connection. A second
browser tab connecting mid-conversation resumes the SAME session rather
than starting a fresh one; multiple simultaneous independent conversations
aren't supported by this skeleton and aren't a real use case yet (evidence
of need, same gate Stage E is held to, would be needed before building that).

No streaming yet: OpenRouterProvider.reply() is a plain blocking POST, so a
turn's reply arrives as one message once ask() completes, same as the CLI's
own UX today. Real token streaming would need OpenRouterProvider itself to
support SSE responses -- a real, separate, out-of-scope-for-this-skeleton
feature, not attempted here.
"""
import asyncio
import json
import threading
import uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from raven.assistant import Assistant
from raven.config import get_api_key, load_settings
from raven.llm_provider import OpenRouterProvider
from raven.store import Store

app = FastAPI()


def _build_assistant(confirm_run_fn, on_tool_call_fn, store) -> Assistant:
    """Same construction shape as cli.py's build_assistant -- provider +
    the given session's Store + settings -- deliberately NOT imported from cli.py: that
    function also assigns cli.py's own ASSISTANT/SETTINGS module globals,
    which assume a single CLI-owned instance. Small enough (a handful of
    lines) that duplicating it here is cheaper and less coupled than
    refactoring cli.py's already-tested global-assignment contract just to
    share it."""
    settings = load_settings()
    model_cfg = settings["model"]
    provider = OpenRouterProvider(
        api_key=get_api_key(),
        model=model_cfg["name"],
        fallbacks=model_cfg.get("fallbacks", []),
    )
    return Assistant(
        provider, confirm_run=confirm_run_fn, on_tool_call=on_tool_call_fn,
        store=store, settings=settings,
    )


MAX_HISTORY_ITEMS = 200


def history_items(history: list[dict]) -> list[dict]:
    """The user-visible part of a conversation history: user prompts and the
    assistant's plain-text replies, oldest first, capped to the most recent
    MAX_HISTORY_ITEMS. Tool results, tool-call-only assistant turns, system
    messages and non-string content (e.g. image parts) are skipped -- the UI
    only needs what a person would have seen in the chat."""
    items = [
        {"role": m["role"], "text": m["content"]}
        for m in history
        if m.get("role") in ("user", "assistant")
        and isinstance(m.get("content"), str) and m["content"].strip()
    ]
    return items[-MAX_HISTORY_ITEMS:]


class Session:
    """One WebSocket connection's bridge into a (synchronous) Assistant.
    Owns the confirm_run/on_tool_call callables Assistant calls from its
    background ask() thread, and the pending-confirmation bookkeeping that
    lets a blocking synchronous call wait on an answer that can only arrive
    asynchronously, over the socket, later."""

    def __init__(self, websocket: WebSocket, loop: asyncio.AbstractEventLoop):
        self.websocket = websocket
        self.loop = loop
        self._pending_confirms: dict[str, tuple[threading.Event, dict]] = {}
        # Strong references to in-flight "ask" tasks -- asyncio only holds a
        # WEAK reference to a task created via create_task, so without this
        # a task can be silently garbage-collected mid-flight.
        self._background_tasks: set[asyncio.Task] = set()
        # One Store per open chat session (see raven/store.py); opens the most
        # recently active session, like the CLI does.
        self.store = Store()
        self.assistant = _build_assistant(self._confirm_run, self._on_tool_call, self.store)

    def _send(self, message: dict) -> None:
        """Fire-and-forget send from a background (non-event-loop) thread."""
        asyncio.run_coroutine_threadsafe(self.websocket.send_json(message), self.loop)

    def _on_tool_call(self, name: str, args: dict) -> None:
        self._send({"type": "tool_call", "name": name, "args": args})

    def _confirm_run(self, prompt: str) -> bool:
        """Called synchronously from Assistant's background ask() thread.
        Sends a confirm_request to the client and BLOCKS (a real,
        synchronous thread block -- safe here since we're never on the
        event loop thread itself) until the matching confirm_response
        arrives via handle_message, or the connection drops."""
        confirm_id = str(uuid.uuid4())
        event = threading.Event()
        answer_box: dict = {"approved": False}
        self._pending_confirms[confirm_id] = (event, answer_box)
        self._send({"type": "confirm_request", "id": confirm_id, "prompt": prompt})
        event.wait()  # woken by handle_message's "confirm_response" branch, or resolve_all_pending on disconnect
        self._pending_confirms.pop(confirm_id, None)
        return answer_box["approved"]

    def resolve_all_pending(self, approved: bool) -> None:
        """Unblocks any confirm_run call still waiting when the connection
        drops -- otherwise that background ask() thread would hang forever
        on a confirmation that can now never arrive. Denies by default
        (approved=False), same "never silently approve" posture headless
        mode already established for exactly this kind of no-one-watching case."""
        for event, answer_box in list(self._pending_confirms.values()):
            answer_box["approved"] = approved
            event.set()

    async def handle_message(self, data: dict) -> None:
        """Dispatches one incoming message. Critically, "ask" must NOT be
        awaited inline here: this method is itself awaited by the receive
        loop in websocket_endpoint, one message at a time -- if handling
        "ask" blocked until Assistant.ask() fully returned, the loop could
        never get back to receive_text() to read an INCOMING
        confirm_response while that same ask() call is sitting in
        confirm_run() waiting for exactly that message. A real deadlock,
        not a hypothetical one -- found by this module's own tests hanging
        outright, not by inspection. Fixed by firing "ask" as a background
        task instead: handle_message returns immediately either way, so the
        receive loop is always free to read the next message concurrently."""
        msg_type = data.get("type")
        if msg_type == "confirm_response":
            confirm_id = data.get("id")
            pending = self._pending_confirms.get(confirm_id)
            if pending:
                event, answer_box = pending
                answer_box["approved"] = bool(data.get("approved"))
                event.set()
            return
        if msg_type == "get_history":
            await self._send_history()
            return
        if msg_type == "list_sessions":
            await self._send_sessions()
            return
        if msg_type in ("new_session", "open_session", "delete_session"):
            if self._background_tasks:
                await self.websocket.send_json(
                    {"type": "error", "message": "Wait for the current request to finish before switching chats."}
                )
                return
            if msg_type == "new_session":
                self._new_session()
            elif msg_type == "open_session":
                if not self._open_session(data.get("id")):
                    await self.websocket.send_json({"type": "error", "message": "That chat no longer exists."})
                    return
            else:
                self._delete_session(data.get("id"))
            await self._send_history()
            await self._send_sessions()
            return
        if msg_type == "ask":
            task = asyncio.create_task(self._run_ask(data.get("text", "")))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            return
        await self.websocket.send_json({"type": "error", "message": f"Unknown message type: {msg_type}"})

    # -- chat sessions -------------------------------------------------------

    def _switch_to(self, session_id: int) -> None:
        """Point this connection at another session: a new Store bound to it and a
        fresh Assistant that loads that session's history."""
        old = self.store
        self.store = Store(session_id=session_id)
        self.assistant = _build_assistant(self._confirm_run, self._on_tool_call, self.store)
        try:
            old.close()
        except Exception:  # noqa: BLE001 -- closing the old connection must never break switching
            pass

    def _new_session(self) -> None:
        # Don't pile up empty chats: if the current one is still empty, it IS the new chat.
        if self.store.message_count() == 0:
            return
        self._switch_to(self.store.create_session())

    def _open_session(self, session_id) -> bool:
        if not any(x["id"] == session_id for x in self.store.list_sessions()):
            return False
        if session_id != self.store.session_id:
            self._switch_to(session_id)
        return True

    def _delete_session(self, session_id) -> None:
        if not any(x["id"] == session_id for x in self.store.list_sessions()):
            return
        was_current = session_id == self.store.session_id
        self.store.delete_session(session_id)
        if was_current:
            remaining = self.store.latest_session_id()
            self._switch_to(remaining if remaining is not None else self.store.create_session())

    async def _send_history(self) -> None:
        await self.websocket.send_json({
            "type": "history", "session": self.store.session_id,
            "items": history_items(self.assistant.history),
        })

    async def _send_sessions(self) -> None:
        await self.websocket.send_json({
            "type": "sessions", "current": self.store.session_id, "items": self.store.list_sessions(),
        })

    async def _run_ask(self, text: str) -> None:
        try:
            reply = await asyncio.to_thread(self.assistant.ask, text)
            await self.websocket.send_json({"type": "reply", "text": reply})
            await self._send_sessions()  # the first message titles the chat; ordering changes too
        except Exception as e:
            await self.websocket.send_json({"type": "error", "message": str(e)})


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    session = Session(websocket, asyncio.get_running_loop())
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON"})
                continue
            await session.handle_message(data)
    except WebSocketDisconnect:
        pass
    finally:
        session.resolve_all_pending(approved=False)


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8756)


if __name__ == "__main__":
    main()
