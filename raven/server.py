"""HTTP/WebSocket API server wrapping Assistant, for the desktop UI
(raven_ui/, Electron+React) -- ... (docstring unchanged) ...
"""
import asyncio
import base64
import json
import logging
import os
import threading
import uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from raven import voice
from raven.assistant import Assistant
from raven.config import get_api_key, get_user_name, load_settings, save_settings
from raven.llm_provider import (
    KNOWN_PROVIDERS, NvidiaProvider, OpenRouterProvider, FreeLLMAPIProvider,
    FREELLMAPI_DEFAULT_BASE_URL, list_nvidia_models, list_freellmapi_models,
)
from raven.store import Store

log = logging.getLogger("raven.server")

app = FastAPI()

STATS_INTERVAL_S = 2.0   # Defines a 2s interval for stats refresh


def _provider_cls_and_kwargs(provider_name: str) -> tuple[type, dict]:
    """Which LLMProvider subclass + extra constructor kwargs for a given
    settings.json model.provider -- the one place this module decides that,
    used by both _build_assistant and _run_set_model so they can't drift
    apart. Branches directly on the provider classes (not
    llm_provider.build_provider's shared PROVIDERS dict) so
    `patch.object(server, "OpenRouterProvider")` keeps working in tests,
    same tradeoff cli.py's own build_assistant makes for the same reason."""
    if provider_name == "nvidia":
        return NvidiaProvider, {}
    if provider_name == "freellmapi":
        # base_url isn't a secret (unlike api_key), but IS machine-specific --
        # the installer's own default port can already be taken by something
        # else (confirmed live on this project's own dev machine), so it's a
        # plain env var with a sensible default, not a settings.json field.
        return FreeLLMAPIProvider, {"base_url": os.environ.get("FREELLMAPI_BASE_URL", FREELLMAPI_DEFAULT_BASE_URL)}
    return OpenRouterProvider, {}


def _build_assistant(confirm_run_fn, on_tool_call_fn, store,
                      on_canvas_update_fn=None, on_canvas_delete_fn=None) -> Assistant:
    """... (unchanged) ... Which LLMProvider subclass gets built (OpenRouter,
    NVIDIA, FreeLLMAPI) is settings.json's model.provider -- see
    _provider_cls_and_kwargs."""
    settings = load_settings()
    model_cfg = settings["model"]
    provider_name = model_cfg.get("provider", "openrouter")
    api_key = get_api_key(provider_name, settings)
    provider_cls, provider_kwargs = _provider_cls_and_kwargs(provider_name)
    provider = provider_cls(
        api_key=api_key, model=model_cfg["name"], fallbacks=model_cfg.get("fallbacks", []), **provider_kwargs
    )
    kwargs = {}
    if on_canvas_update_fn is not None:
        kwargs["on_canvas_update"] = on_canvas_update_fn
    if on_canvas_delete_fn is not None:
        kwargs["on_canvas_delete"] = on_canvas_delete_fn
    return Assistant(
        provider, confirm_run=confirm_run_fn, on_tool_call=on_tool_call_fn,
        store=store, settings=settings, **kwargs,
    )


class _AskToken:
    """One per in-flight ask() call, so cancel_ask() marks exactly THAT call
    cancelled -- never a later one. Needed because Assistant.ask() runs in a
    real OS thread (asyncio.to_thread): unlike the CLI's own Ctrl+C (a signal
    the OS delivers into the blocking call itself), nothing here can forcibly
    stop that thread mid-network-call. Cancelling instead marks the token and
    waits for the call to finish naturally; _run_ask then rolls the turn back
    from history rather than reporting it, the same "half-finished turn can't
    pollute later context" guarantee Assistant.ask's own KeyboardInterrupt
    handler already gives the CLI."""

    def __init__(self):
        self.cancelled = False


MAX_HISTORY_ITEMS = 200


def history_items(history: list[dict]) -> list[dict]:
    """... (unchanged) ..."""
    items = [
        {"role": m["role"], "text": m["content"]}
        for m in history
        if m.get("role") in ("user", "assistant")
        and isinstance(m.get("content"), str) and m["content"].strip()
    ]
    return items[-MAX_HISTORY_ITEMS:]


class Session:
    """... (docstring unchanged) ..."""

    def __init__(self, websocket: WebSocket, loop: asyncio.AbstractEventLoop):
        self.websocket = websocket
        self.loop = loop
        self._pending_confirms: dict[str, tuple[threading.Event, dict]] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self.store = Store()
        self.assistant = _build_assistant(
            self._confirm_run, self._on_tool_call, self.store,
            self._on_canvas_update, self._on_canvas_delete,
        )
        self._stats_task: asyncio.Task | None = None   # NEW
        self._ask_token: _AskToken | None = None  # the in-flight ask(), if any -- see _AskToken

    # -- stats push ----------------------------------------------------------

    def start_stats_loop(self) -> None:                # NEW
        """Kick off the periodic statusline push. Called once from the
        endpoint right after the socket is accepted; stopped in the same
        finally block that resolves pending confirms."""
        self._stats_task = asyncio.create_task(self._stats_loop())

    def stop_stats_loop(self) -> None:                 # NEW
        if self._stats_task:
            self._stats_task.cancel()
            self._stats_task = None

    async def _stats_loop(self) -> None:               # NEW
        """Push a statusline snapshot every STATS_INTERVAL_S while the socket
        is open. Timer-driven rather than reply-driven because cpu/mem
        change on their own between turns. Runs on the event loop (unlike
        _run_ask): a fast psutil read plus an async send, nothing to offload.

        Sleeps FIRST, then sends -- not the other way around. This task starts
        immediately on connect, at the same moment _send_history() (also
        triggered on connect, by the client's own get_history) sends its own
        explicit stats snapshot; sending here too, immediately, raced that
        explicit push on every fresh connection with no guaranteed order,
        which is genuinely redundant (both would report the same near-instant
        state) and made "connect, send one message, expect the very next
        message to be its reply" -- true of nearly every caller -- fail
        unpredictably (found via a real CI run turning red across all three
        OSes). Deferring the first tick by a full interval leaves that
        request/reply exchange an interval-sized window with no competing
        background push."""
        while True:
            await asyncio.sleep(STATS_INTERVAL_S)
            try:
                await self._send_stats()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("stats push failed")     # NEW -- visible in terminal

    async def _send_stats(self) -> None:               # FIXED
        from raven import statusline
        # .get() chain so a config with no statusline block falls back to
        # every segment statusline defines, instead of raising KeyError and
        # killing the tick silently.
        names = (self.assistant.settings.get("statusline", {}).get("segments")
                 or list(statusline.SEGMENTS))
        segments = []
        for name in names:
            fn = statusline.SEGMENTS.get(name)
            if fn is None:
                log.warning("unknown statusline segment: %r", name)
                continue
            try:
                text = fn(self.assistant.provider, self.assistant)
                # A segment can fail WITHOUT raising: e.g. a test's mocked provider
                # (or any future provider missing an expected attribute/method)
                # auto-generates further mocks for any attribute access or call,
                # so a chain like `provider.last_model.split("/")[-1]` returns
                # another mock object silently instead of erroring. That mock is
                # not JSON-serializable, and unlike an exception here, a bad
                # VALUE was never being caught -- it reached send_json() below
                # and crashed the whole websocket connection (found via a real
                # CI run: every test_server.py test failed this way, not just
                # the ones actually about stats). Guarding the type turns that
                # into the same "skip this one segment" outcome as any other
                # segment failure.
                if not isinstance(text, str):
                    raise TypeError(f"segment {name!r} returned {type(text).__name__}, not str")
                segments.append({"name": name, "text": text})
            except Exception:                          # noqa: BLE001
                log.exception("statusline segment %r failed", name)
        await self.websocket.send_json({"type": "stats", "segments": segments})

    # -- existing methods (unchanged) ---------------------------------------

    def _send(self, message: dict) -> None:
        asyncio.run_coroutine_threadsafe(self.websocket.send_json(message), self.loop)

    def _on_tool_call(self, name: str, args: dict) -> None:
        self._send({"type": "tool_call", "name": name, "args": args})

    def _on_canvas_update(self, element: dict) -> None:
        self._send({"type": "canvas_update", "element": element})

    def _on_canvas_delete(self, element_id: str) -> None:
        self._send({"type": "canvas_delete", "element_id": element_id})

    def _confirm_run(self, prompt: str) -> bool:
        # A tool call that needs confirmation AFTER the user already cancelled
        # this ask must never reach them for a real decision -- cancelling
        # means "stop, discard whatever happens," not "pause until I decide
        # whether this one specific action should still run." Auto-deny
        # instead of sending confirm_request at all, so no dialog appears and
        # the background thread doesn't sit waiting on a human who already
        # said they're done with this turn.
        if self._ask_token is not None and self._ask_token.cancelled:
            return False
        confirm_id = str(uuid.uuid4())
        event = threading.Event()
        answer_box: dict = {"approved": False}
        self._pending_confirms[confirm_id] = (event, answer_box)
        self._send({"type": "confirm_request", "id": confirm_id, "prompt": prompt})
        event.wait()
        self._pending_confirms.pop(confirm_id, None)
        return answer_box["approved"]

    def resolve_all_pending(self, approved: bool) -> None:
        # ... (unchanged) ...
        for event, answer_box in list(self._pending_confirms.values()):
            answer_box["approved"] = approved
            event.set()

    async def handle_message(self, data: dict) -> None:
        # ... (unchanged) ...
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
            if self._ask_token is not None:
                # Nothing currently stops two "ask" calls from mutating
                # self.assistant.history concurrently from two different
                # threads -- refusing a second one while the first (cancelled
                # or not) hasn't actually settled yet is what keeps that safe.
                await self.websocket.send_json(
                    {"type": "error", "message": "Wait for the current request to finish (or cancel it) before sending another."}
                )
                return
            token = self._ask_token = _AskToken()
            task = asyncio.create_task(self._run_ask(data.get("text", ""), token))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            return
        if msg_type == "cancel_ask":
            if self._ask_token is None:
                await self.websocket.send_json({"type": "error", "message": "Nothing to cancel."})
                return
            self._ask_token.cancelled = True
            # A confirm_request from THIS turn may already be sitting in
            # front of the user right now -- auto-deny it immediately rather
            # than leaving it up to them (see _confirm_run's own matching
            # check for one requested AFTER this point). Safe to resolve
            # every pending confirm, not just "this turn's": only one ask
            # runs at a time (enforced above), so anything still pending here
            # belongs to the turn being cancelled.
            self.resolve_all_pending(approved=False)
            # Acknowledges the request was received -- NOT that the turn has
            # settled (the underlying call is still genuinely running; only
            # _run_ask's own "cancelled" message, once it actually finishes,
            # means the UI is free to send another message).
            await self.websocket.send_json({"type": "cancel_requested"})
            return
        if msg_type == "transcribe":
            # Independent of ask()/session switching -- it never touches self.assistant
            # or self.store, so it's fine to run alongside either without the
            # "wait for the current request" guard those use.
            task = asyncio.create_task(self._run_transcribe(data.get("audio", ""), data.get("id")))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            return
        if msg_type == "get_model_config":
            await self._send_model_config()
            return
        if msg_type == "list_models":
            task = asyncio.create_task(self._run_list_models(data.get("provider", "")))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            return
        if msg_type == "set_model":
            await self._run_set_model(data.get("provider", ""), data.get("name", ""))
            return
        if msg_type == "get_user_config":
            await self._send_user_config()
            return
        if msg_type == "set_api_key":
            await self._run_set_api_key(data.get("provider", ""), data.get("key", ""))
            return
        if msg_type == "set_user_name":
            await self._run_set_user_name(data.get("name", ""))
            return
        if msg_type == "get_canvas":
            await self._send_canvas()
            return
        if msg_type == "canvas_edit":
            # A hand-edit (drag/resize/rotate/recolor/reparametrize/reorder)
            # bypasses the model entirely -- Assistant.apply_canvas_edit
            # persists it and (via _on_canvas_update, bound in __init__)
            # pushes the authoritative, regenerated element back, which is
            # also how the sender's own optimistic UI state reconciles.
            el = self.assistant.apply_canvas_edit(data.get("element_id", ""), data.get("patch") or {})
            if el is None:
                await self.websocket.send_json({"type": "error", "message": "No such canvas element."})
            return
        if msg_type == "canvas_delete":
            if not self.assistant.delete_canvas_element_by_user(data.get("element_id", "")):
                await self.websocket.send_json({"type": "error", "message": "No such canvas element."})
            return
        if msg_type == "canvas_restore":
            # The undo half of canvas_delete -- the client kept the exact
            # element it asked to delete, so undo just hands it straight
            # back rather than the server needing its own undo history.
            element = data.get("element") or {}
            if not isinstance(element, dict) or "id" not in element:
                await self.websocket.send_json({"type": "error", "message": "Invalid element to restore."})
                return
            self.assistant.restore_element(element)
            return
        if msg_type == "canvas_group":
            if self.assistant.group_elements(data.get("element_ids") or []) is None:
                await self.websocket.send_json(
                    {"type": "error", "message": "Need at least 2 existing elements to group."}
                )
            return
        if msg_type == "canvas_ungroup":
            if self.assistant.ungroup_elements(data.get("group_id", "")) is None:
                await self.websocket.send_json({"type": "error", "message": "No such group."})
            return
        await self.websocket.send_json({"type": "error", "message": f"Unknown message type: {msg_type}"})

    # -- chat sessions (unchanged) ------------------------------------------

    def _switch_to(self, session_id: int) -> None:
        old = self.store
        self.store = Store(session_id=session_id)
        self.assistant = _build_assistant(
            self._confirm_run, self._on_tool_call, self.store,
            self._on_canvas_update, self._on_canvas_delete,
        )
        try:
            old.close()
        except Exception:  # noqa: BLE001
            pass

    def _new_session(self) -> None:
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
        await self._send_canvas()                       # Design mode: restore this session's canvas too
        await self._send_stats()                       # NEW -- populate on connect / switch

    async def _send_sessions(self) -> None:
        await self.websocket.send_json({
            "type": "sessions", "current": self.store.session_id, "items": self.store.list_sessions(),
        })

    async def _send_canvas(self) -> None:
        await self.websocket.send_json({
            "type": "canvas", "session": self.store.session_id, "elements": self.assistant.canvas,
        })

    async def _run_ask(self, text: str, token: _AskToken) -> None:
        checkpoint = len(self.assistant.history)
        try:
            reply = await asyncio.to_thread(self.assistant.ask, text)
        except Exception as e:
            self._ask_token = None
            # A cancelled call that then also happens to fail (e.g. the model
            # request errors out after the user gave up on it) is still just
            # a cancellation from the user's point of view -- no need to show
            # a scary error for something they already dismissed.
            if not token.cancelled:
                await self.websocket.send_json({"type": "error", "message": str(e)})
            else:
                await self.websocket.send_json({"type": "cancelled"})
            return
        self._ask_token = None
        if token.cancelled:
            # The call ran to completion regardless (a real OS thread can't be
            # forcibly interrupted) -- roll the turn back exactly like the
            # CLI's own Ctrl+C/KeyboardInterrupt handling in Assistant.ask,
            # and never report the answer the user already dismissed. KNOWN
            # LIMITATION, stated plainly rather than silently accepted:
            # Assistant.ask() already called its own _persist() before
            # returning here, so the discarded turn is still sitting in the
            # SQLite store on disk even though it's now gone from the live
            # self.assistant.history -- this session's UI/model context is
            # correct either way, but a full app restart afterward would load
            # the "cancelled" turn back from disk. The CLI's own Ctrl+C
            # avoids this entirely (there, cancellation is a real
            # KeyboardInterrupt raised INSIDE _run_turn, which skips
            # Assistant.ask's _persist() call by propagating out before
            # reaching it) -- not available here since a background OS thread
            # can't be interrupted that way. Fixing this fully would need a
            # new Store method to delete specific rows after the fact; not
            # done, since the common case (keep using this session) is
            # already correct.
            del self.assistant.history[checkpoint:]
            await self.websocket.send_json({"type": "cancelled"})
            return
        await self.websocket.send_json({"type": "reply", "text": reply})
        await self._send_sessions()
        await self._send_stats()                   # NEW -- refresh token/context at turn end

    # -- voice input -----------------------------------------------------------
    # Push-to-talk from the desktop UI: the renderer captures raw audio (Web
    # Audio API), never a compressed format, so the exact same offline
    # faster-whisper pipeline the CLI's Ctrl+V already uses (raven/voice.py)
    # can transcribe it directly -- no ffmpeg, no cloud call, one code path for
    # both surfaces. `id` round-trips the client's request so out-of-order
    # replies (unlikely, but the client doesn't serialize push-to-talk
    # presses) still resolve the right pending promise.

    async def _run_transcribe(self, audio_b64: str, request_id) -> None:
        if not voice.is_available():
            await self.websocket.send_json({
                "type": "transcript_error", "id": request_id,
                "message": ('Voice dependencies aren\'t installed -- pip install ".[voice]" '
                            "(and on Linux: sudo apt install libportaudio2)."),
            })
            return
        if not voice.model_ready():
            await self.websocket.send_json({
                "type": "transcript_error", "id": request_id,
                "message": "Voice model not downloaded -- run: python -m raven.voice",
            })
            return
        try:
            import numpy as np
            # 16-bit PCM little-endian mono at voice.SAMPLE_RATE, exactly what the
            # renderer's AudioContext({sampleRate: voice.SAMPLE_RATE}) produces --
            # see raven_ui/src/stages/VoiceStage.tsx. Normalized to float32 in
            # [-1, 1], the same shape Recorder.stop() hands transcribe() on the CLI.
            samples = np.frombuffer(base64.b64decode(audio_b64), dtype="<i2").astype("float32") / 32768.0
            text = await asyncio.to_thread(voice.transcribe, samples)
        except voice.ModelNotDownloadedError as e:
            await self.websocket.send_json({"type": "transcript_error", "id": request_id, "message": str(e)})
            return
        except Exception as e:  # noqa: BLE001 -- always report to the UI, never drop the request silently
            await self.websocket.send_json(
                {"type": "transcript_error", "id": request_id, "message": f"Couldn't transcribe: {e}"}
            )
            return
        await self.websocket.send_json({"type": "transcript", "id": request_id, "text": text})

    # -- model provider/name (desktop Settings panel) ---------------------------
    # Lets the user pick a different provider (OpenRouter, NVIDIA) and model
    # from the UI instead of hand-editing settings.json. `set_model` persists
    # the choice (so it survives a restart, same as any other setting) AND
    # takes effect immediately in this running session by rebuilding
    # self.assistant, exactly like switching chats already does in _switch_to.

    async def _send_model_config(self) -> None:
        model_cfg = load_settings()["model"]
        await self.websocket.send_json({
            "type": "model_config", "provider": model_cfg.get("provider", "openrouter"), "name": model_cfg["name"],
        })

    async def _run_list_models(self, provider_name: str) -> None:
        if provider_name not in ("nvidia", "freellmapi"):
            # OpenRouter's model is still chosen by editing settings.json
            # directly (see README) -- it has no listable-catalog endpoint
            # this picker uses.
            await self.websocket.send_json({
                "type": "models_error", "provider": provider_name,
                "message": f"Listing models isn't supported for provider {provider_name!r}.",
            })
            return
        try:
            api_key = get_api_key(provider_name, load_settings())
            if provider_name == "freellmapi":
                base_url = os.environ.get("FREELLMAPI_BASE_URL", FREELLMAPI_DEFAULT_BASE_URL)
                models = await asyncio.to_thread(list_freellmapi_models, api_key, base_url)
            else:
                models = await asyncio.to_thread(list_nvidia_models, api_key)
        except Exception as e:  # noqa: BLE001 -- always report to the UI, never drop the request silently
            await self.websocket.send_json({"type": "models_error", "provider": provider_name, "message": str(e)})
            return
        await self.websocket.send_json({"type": "models", "provider": provider_name, "items": models})

    async def _run_set_model(self, provider_name: str, name: str) -> None:
        if provider_name not in KNOWN_PROVIDERS:
            await self.websocket.send_json({
                "type": "error",
                "message": f"Unknown model provider: {provider_name!r}. Known providers: {', '.join(KNOWN_PROVIDERS)}.",
            })
            return
        if not name.strip():
            await self.websocket.send_json({"type": "error", "message": "A model name is required."})
            return
        settings = load_settings()
        was_provider = settings["model"].get("provider", "openrouter")
        # Fallbacks are model ids for the OLD provider -- carrying them across a
        # provider change would silently feed e.g. an OpenRouter id to NVIDIA's
        # API. This picker has no fallback UI of its own, so the safe default
        # is to clear them on an actual provider switch and leave them alone
        # when just picking a different model from the same provider.
        new_fallbacks = settings["model"].get("fallbacks", []) if provider_name == was_provider else []
        new_model_cfg = {**settings["model"], "provider": provider_name, "name": name, "fallbacks": new_fallbacks}
        try:
            api_key = get_api_key(provider_name, settings)
            provider_cls, provider_kwargs = _provider_cls_and_kwargs(provider_name)
            new_provider = provider_cls(api_key=api_key, model=name, fallbacks=new_fallbacks, **provider_kwargs)
        except Exception as e:  # noqa: BLE001 -- validated BEFORE persisting/swapping, so a bad
            # choice (missing key, ...) leaves the current, working model untouched.
            await self.websocket.send_json({"type": "error", "message": str(e)})
            return
        settings["model"] = new_model_cfg
        save_settings(settings)
        self.assistant.provider = new_provider
        await self._send_model_config()
        await self._send_stats()  # refresh the "Current model" segment immediately

    # -- user name + API keys (desktop Settings panel) --------------------------
    # Lets a fresh install be fully configured from the UI -- no .env file, no
    # hand-edited settings.json. The key itself is never echoed back to the
    # client once saved (_send_user_config reports only whether one is SET per
    # provider, not its value) -- same "don't round-trip secrets" posture as
    # every other credential in this project.

    async def _send_user_config(self) -> None:
        settings = load_settings()
        has_key = {p: bool(settings.get("api_keys", {}).get(p, "").strip()) for p in KNOWN_PROVIDERS}
        await self.websocket.send_json({
            "type": "user_config", "name": get_user_name(settings), "has_key": has_key,
        })

    async def _run_set_api_key(self, provider_name: str, key: str) -> None:
        if provider_name not in KNOWN_PROVIDERS:
            await self.websocket.send_json({
                "type": "error",
                "message": f"Unknown model provider: {provider_name!r}. Known providers: {', '.join(KNOWN_PROVIDERS)}.",
            })
            return
        if not key.strip():
            await self.websocket.send_json({"type": "error", "message": "An API key is required."})
            return
        settings = load_settings()
        settings.setdefault("api_keys", {})[provider_name] = key.strip()
        save_settings(settings)
        # Apply immediately if this is the provider currently in use -- same
        # "takes effect now, not just after a restart" posture as set_model.
        if provider_name == settings["model"].get("provider", "openrouter"):
            provider_cls, provider_kwargs = _provider_cls_and_kwargs(provider_name)
            self.assistant.provider = provider_cls(
                api_key=key.strip(), model=settings["model"]["name"],
                fallbacks=settings["model"].get("fallbacks", []), **provider_kwargs,
            )
        await self._send_user_config()

    async def _run_set_user_name(self, name: str) -> None:
        settings = load_settings()
        settings.setdefault("user", {})["name"] = name.strip()
        save_settings(settings)
        await self._send_user_config()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    session = Session(websocket, asyncio.get_running_loop())
    session.start_stats_loop()                         # NEW
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
        session.stop_stats_loop()                      # NEW
        session.resolve_all_pending(approved=False)


def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(app, host="127.0.0.1", port=8756)


if __name__ == "__main__":
    main()