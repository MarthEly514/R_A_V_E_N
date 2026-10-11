"""Conversation state and tool-calling orchestration."""
import base64
import json
import math
import mimetypes
import re
import time
from pathlib import Path
from typing import Callable

from raven import config
from raven import design as design_impl
from raven.llm_provider import LLMProvider, SKILL_PROMPTS
from raven.store import Store
from raven import gmail as gmail_impl
from raven import platforms as plat
from raven import tools as tool_impl

MAX_TOOL_ITERATIONS = 300  # was 5; browser workflows (navigate/read/act/verify) routinely need more
# ~12k tokens (chars/4 approximation) of history sent per request. This is a
# practicality budget, not a model context-window limit — the free models
# behind the default fallback list have context windows in the hundreds of
# thousands of tokens; a smaller request is just faster on a slow free tier.
# Replaces the old flat MAX_CONTEXT_MESSAGES=40 message-count cap (A2): a
# handful of messages can now vary wildly in size since tool outputs are
# capped individually (tools.MAX_OUTPUT_CHARS) but there's no cap on how many
# of them accumulate, so counting messages under- or over-shoots the actual
# request size depending on what's in them.
MAX_CONTEXT_CHARS = 48_000
# Compaction (B2): once the budget above would otherwise DROP history, that
# dropped prefix is summarized instead of silently lost from what's sent.
# The summary itself is capped so it stays genuinely space-saving and can't
# grow unbounded across many compaction cycles over a very long-lived history.
MAX_SUMMARY_CHARS = 2000
# Design mode: caps the injected "canvas changed since your last turn" note
# (see Assistant._pop_canvas_delta_summary) -- a burst of drags/resizes
# shouldn't be able to grow this without bound.
MAX_CANVAS_DELTA_CHARS = 800
SAFE_GIT = {"status", "log", "diff", "show"}  # read-only git subcommands, no confirmation

# run_command: read-only, side-effect-free programs that skip confirmation when
# invoked plainly (no shell chaining/redirection/substitution — see _is_safe_command).
SAFE_COMMANDS = {
    "ls", "pwd", "whoami", "date", "uname", "df", "du", "ps",
    "wc", "which", "cat", "head", "tail", "echo", "id", "hostname", "free", "uptime",
}
# Any of these anywhere in a command string means "don't trust a first-word allowlist
# check" — chaining (; | &), redirection (< >), or substitution (` $ ( )) can hide an
# unsafe command behind a safe-looking start, e.g. "git log ; rm -rf ~" or "ls > file".
_SHELL_METACHARS = set(";|&<>`$()\n")

# Windows (cmd.exe) equivalents of the read-only programs above. Only consulted
# on Windows; the Unix names stay in the set too, since Git Bash/WSL tools may exist.
SAFE_COMMANDS_WINDOWS = {"dir", "type", "where", "findstr", "ver", "hostname", "whoami"}
# cmd.exe has extra metacharacters: `^` (escape) and `%VAR%` (expansion -- which
# happens even inside double quotes) -- rejected anywhere on Windows.
_WINDOWS_EXTRA_METACHARS = set("^%")

# Read-only programs that are only safe with certain arguments, or that are only
# meaningful as part of a pipeline (grep/sort/cut...). Each maps to a checker
# over the argument list (program name excluded). Everything here writes nothing
# and executes nothing -- e.g. NOT `find -exec/-delete`, `sort -o`, `sed -i`.
_SED_PRINT_RE = re.compile(r"^(\d+|\$|/[^/]*/)?(,(\d+|\$|/[^/]*/))?p$")


def _args_ok_find(args):
    return not any(a in ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf", "-fls")
                   for a in args)


def _args_ok_sort(args):
    # Windows `sort /O file` writes its output to a file.
    if plat.is_windows() and any(a.lower().startswith("/o") for a in args):
        return False
    return not any(a == "-o" or a.startswith("--output") or (a.startswith("-") and not a.startswith("--") and "o" in a[1:])
                   for a in args)


def _args_ok_sed(args):
    # only the print-a-range form: sed -n '10,20p' / '/start/,/end/p' -- never -i/-e/-f or w/e commands
    if "-n" not in args:
        return False
    rest = [a for a in args if a != "-n"]
    return bool(rest) and bool(_SED_PRINT_RE.match(rest[0])) and not any(a.startswith("-") for a in rest[:1])


def _args_ok_pdftotext(args):
    # writes a file unless the output target is stdout ("-")
    return len(args) >= 2 and args[-1] == "-"


_CONDITIONALLY_SAFE = {
    "find": _args_ok_find, "sort": _args_ok_sort, "sed": _args_ok_sed, "pdftotext": _args_ok_pdftotext,
}
# Always-safe read-only filters/inspectors (beyond SAFE_COMMANDS).
_SAFE_FILTERS = {"grep", "egrep", "fgrep", "uniq", "cut", "tr", "file", "stat", "pdfinfo", "basename", "dirname", "realpath", "nl"}


def _split_safe_pipeline(command: str) -> list[str] | None:
    """Split `command` on unquoted `|` into segments, or None if anything
    other than plain words/quoted strings/`|`/a stderr redirect appears
    outside quotes (chaining, other redirection, substitution...).

    POSIX shells: single quotes are fully literal; inside double quotes a
    backtick or a `$` that starts an expansion is still rejected. Allowed
    redirects: `2>/dev/null`, `2>&1`.
    Windows (cmd.exe): only DOUBLE quotes quote anything (a single quote is
    an ordinary character, so `'a|b'` really pipes); `^` and `%` are
    rejected everywhere (escape / %VAR% expansion, which applies inside
    quotes too). Allowed redirects: `2>nul`, `2>&1`."""
    windows = plat.is_windows()
    stderr_redirects = ("2>nul", "2>&1") if windows else ("2>/dev/null", "2>&1")
    if windows and any(c in _WINDOWS_EXTRA_METACHARS for c in command):
        return None
    segments, cur, i, n = [], [], 0, len(command)
    while i < n:
        c = command[i]
        if c == "'" and not windows:
            j = command.find("'", i + 1)
            if j == -1:
                return None
            cur.append(command[i:j + 1]); i = j + 1
        elif c == '"':
            j = i + 1
            while j < n and command[j] != '"':
                if command[j] == "\\" and not windows:
                    j += 1
                elif command[j] == "`":
                    return None
                elif command[j] == "$" and j + 1 < n and command[j + 1] != '"' and not windows:
                    return None
                j += 1
            if j >= n:
                return None
            cur.append(command[i:j + 1]); i = j + 1
        elif c == "\\" and not windows:
            if i + 1 >= n or command[i + 1] == "\n":
                return None
            cur.append(command[i:i + 2]); i += 2
        elif c == "|":
            if i + 1 < n and command[i + 1] == "|":
                return None
            segments.append("".join(cur)); cur = []; i += 1
        elif any(command[i:i + len(r)].lower() == r for r in stderr_redirects):
            i += next(len(r) for r in stderr_redirects if command[i:i + len(r)].lower() == r)
        elif c in _SHELL_METACHARS:
            return None
        else:
            cur.append(c); i += 1
    segments.append("".join(cur))
    return segments


def _segment_is_safe(segment: str, allowed: frozenset[str]) -> bool:
    import shlex
    windows = plat.is_windows()
    try:
        # posix=False on Windows so backslashes in paths (C:\Users\x) survive.
        words = shlex.split(segment, posix=not windows)
    except ValueError:
        return False
    if not words:
        return False
    program = words[0].strip('"').replace("\\", "/").rsplit("/", 1)[-1]
    if windows:
        program = program.lower()
        if program.endswith(".exe"):
            program = program[:-4]
    args = [a.strip('"') for a in words[1:]] if windows else words[1:]
    if program in _CONDITIONALLY_SAFE:
        return _CONDITIONALLY_SAFE[program](args)
    safe = SAFE_COMMANDS | SAFE_COMMANDS_WINDOWS if windows else SAFE_COMMANDS
    return program in safe or program in _SAFE_FILTERS or program in allowed


# D2: the one tool always offered beyond tool_impl.core_tool_names() -- lives
# here (not in tools.py's TOOL_SPECS/TOOL_FUNCTIONS) since it's not a "operate
# on the world" primitive but Assistant-instance state (self.active_skills),
# handled specially in _run_tool before the generic TOOL_FUNCTIONS dispatch.
LOAD_SKILL_SPEC = {"type": "function", "function": {
    "name": "load_skill",
    "description": (
        "Unlock additional tools for a specific area not currently in your tool list -- call "
        "this BEFORE attempting to use a tool outside your current list, then use the "
        "newly-available tools normally. Available skills: "
        + "; ".join(f"{name} ({desc})" for name, desc in tool_impl.SKILL_DESCRIPTIONS.items())
    ),
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string", "enum": list(tool_impl.SKILLS.keys()),
                  "description": "Which skill to load"}},
        "required": ["name"]},
}}

# Free/small models routinely ignore the "no emojis" system-prompt rule, so it's
# enforced here instead of trusting instruction-following. Ranges given as
# (start, end) codepoints to avoid embedding literal emoji/invisible chars in source.
_EMOJI_RANGES = [
    (0x1F1E6, 0x1F1FF),  # regional indicator flags
    (0x1F300, 0x1FAFF),  # symbols & pictographs, emoticons, transport, supplemental
    (0x2600, 0x27BF),    # misc symbols, dingbats
    (0x2B00, 0x2BFF),    # misc symbols and arrows
    (0xFE0F, 0xFE0F),    # variation selector-16
    (0x200D, 0x200D),    # zero-width joiner
]
_EMOJI_RE = re.compile(
    "[" + "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in _EMOJI_RANGES) + "]+"
)


def strip_emoji(text: str) -> str:
    return _EMOJI_RE.sub("", text)


NOTIFICATION_BODY_CHARS = 200  # OS notification toasts are single-glance, not a place to read a full reply


def _notification_body(reply: str) -> str:
    text = " ".join(reply.split())  # collapse newlines/indentation -- a toast is one line
    if len(text) <= NOTIFICATION_BODY_CHARS:
        return text
    return text[:NOTIFICATION_BODY_CHARS].rstrip() + "…"


def _coerce_numeric_strings(value):
    """Recursively turns a JSON string that's actually a number (e.g. "80",
    from a model that ignored the tool spec's declared "number"/"integer"
    type -- a real, live-confirmed behavior, not a hypothetical) into a real
    int/float, anywhere in a (possibly nested) tool-call argument structure.
    A string that genuinely isn't numeric (a color, a motif name, a shape
    id) simply fails to parse and is returned unchanged -- this never
    touches anything that wasn't already a number written as text."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            pass
        try:
            return float(value)
        except ValueError:
            return value
    if isinstance(value, list):
        return [_coerce_numeric_strings(v) for v in value]
    if isinstance(value, dict):
        return {k: _coerce_numeric_strings(v) for k, v in value.items()}
    return value


_INVISIBLE = (None, "", "none", "transparent")
FALLBACK_STROKE = "#141414"  # matches place_element's own default fill color


def _ensure_visible(el: dict) -> None:
    """A model can (and, live-confirmed, does) choose fill="none" for an
    outline-only shape while ALSO leaving stroke as "none" (its own
    default) -- the tool call succeeds and reports "Drew ...", but nothing
    is actually visible on the canvas, which looks EXACTLY like the
    drawing never happened at all from the user's point of view. Mutates
    `el` in place to add a visible fallback stroke only in that specific
    case -- a shape that deliberately sets a real stroke or fill is left
    untouched."""
    if (el.get("fill") or "").lower() in _INVISIBLE and (el.get("stroke") or "").lower() in _INVISIBLE:
        el["stroke"] = FALLBACK_STROKE
        if not el.get("stroke_width"):
            el["stroke_width"] = 1.0


def _has_shell_metachars(s: str) -> bool:
    return any(c in _SHELL_METACHARS for c in s)


def _build_compaction_prompt(prior_summary: str | None, new_messages: list[dict]) -> str:
    transcript = "\n".join(
        f"{m.get('role', '?')}: {m['content']}" for m in new_messages if m.get("content")
    )
    instructions = (
        "Keep concrete facts, decisions, and open threads; drop small talk and "
        "resolved back-and-forth. Reply with the summary only, no preamble."
    )
    if prior_summary:
        return (
            f"Update this summary of an earlier conversation with the new messages "
            f"below, so it stays a single concise summary of everything so far. "
            f"{instructions}\n\nEXISTING SUMMARY:\n{prior_summary}\n\n"
            f"NEW MESSAGES TO FOLD IN:\n{transcript}"
        )
    return f"Summarize the following conversation concisely. {instructions}\n\nCONVERSATION:\n{transcript}"


class Assistant:
    def __init__(
        self,
        provider: LLMProvider,
        confirm_run: Callable[[str], bool] = lambda cmd: True,
        store: Store | None = None,
        on_tool_call: Callable[[str, dict], None] = lambda name, args: None,
        on_canvas_update: Callable[[dict], None] = lambda el: None,
        on_canvas_delete: Callable[[str], None] = lambda element_id: None,
        settings: dict | None = None,
    ):
        self.provider = provider
        self.confirm_run = confirm_run
        self.store = store
        self.settings = settings or {}
        self.on_tool_call = on_tool_call  # notified (name, args) right before each tool runs
        # Design mode: notified with the full element right after it's
        # created/edited/grouped/ungrouped, and with just an id on delete --
        # mirrors on_tool_call's shape (server.py binds these to websocket
        # pushes; the CLI leaves them as no-ops, same "no rich UI, no push"
        # posture analyze_image/browser tools already have).
        self.on_canvas_update = on_canvas_update
        self.on_canvas_delete = on_canvas_delete
        # C3: user-configured trust, ADDITIVE to SAFE_COMMANDS/SAFE_GIT (never
        # replaces them, and never bypasses the shell-metachar check below).
        permissions = (settings or {}).get("permissions", {})
        self.allowed_commands = frozenset(permissions.get("allow_commands", []))
        self.allowed_git = frozenset(permissions.get("allow_git", []))
        # D1: user-configured automation, same trust boundary as permissions
        # above (settings.json is a local file the user controls) -- never
        # confirmed, since being listed here IS the user's approval.
        self.hooks = (settings or {}).get("hooks", {"pre": {}, "post": {}})
        # D2: which skills this SESSION has unlocked so far (see load_skill).
        # Purely in-memory, never persisted -- a fresh session always starts
        # back at core-only, same reasoning as B2's compacted summary: Store
        # stays the simple, permanent ground truth, and re-loading a skill
        # if/when it's next needed is cheap and correct either way.
        self.active_skills: set[str] = set()
        # V2: a SEPARATE model for analyze_image -- the main chat model isn't
        # necessarily vision-capable. Resolved against the ACTIVE provider
        # (config.get_vision_model) rather than read as a flat value -- see
        # that function's own docstring and DEFAULT_SETTINGS["model"]["vision"]
        # for why a single hardcoded slug silently breaks the moment
        # model.provider changes. None (no settings, or nothing configured
        # for this provider) means analyze_image reports a clear error
        # rather than trying and failing against a model that can't handle
        # images.
        self.vision_model = config.get_vision_model(settings or {})
        self.history: list[dict] = store.load() if store else []
        self._persisted = len(self.history)
        # Design mode's canvas: persisted per-session exactly like history
        # (store.load_canvas/save_canvas), so switching chats switches
        # canvases too. _pending_canvas_changes accumulates a plain-English
        # note per USER hand-edit (never R.A.V.E.N's own tool calls, which
        # she already knows about) -- _context() prepends and clears it as a
        # one-off summary message, so her next turn isn't reasoning from a
        # canvas state she doesn't know changed (same "inject a summary"
        # shape compaction already uses for dropped history, see _context).
        self.canvas: list[dict] = store.load_canvas() if store else []
        self._pending_canvas_changes: list[str] = []
        self.last_elapsed = 0.0  # wall time of the last completed ask(), in seconds
        self.last_reasoning: list[str] = []  # each call's reasoning trace, if the model gave one
        self._compacted_summary: str | None = None  # running summary of history[:self._compacted_through]
        self._compacted_through: int = 0  # self.history is NEVER mutated by compaction (see compact())

    def ask(self, user_input: str) -> str:
        checkpoint = len(self.history)
        self.history.append({"role": "user", "content": user_input})
        t0 = time.monotonic()
        try:
            reply = self._run_turn()
        except KeyboardInterrupt:
            del self.history[checkpoint:]
            raise
        self.last_elapsed = time.monotonic() - t0
        self.last_reasoning = [
            m["reasoning"] for m in self.history[checkpoint:]
            if m.get("role") == "assistant" and m.get("reasoning")
        ]
        self._persist()
        if reply:
            plat.current().notify("R.A.V.E.N", _notification_body(reply))
        return reply

    def _run_turn(self) -> str:
        for _ in range(MAX_TOOL_ITERATIONS):
            message = self.provider.reply(self._context(), tools=self._active_tool_specs())
            if message.get("content"):
                message["content"] = strip_emoji(message["content"])
            if message.get("reasoning"):
                message["reasoning"] = strip_emoji(message["reasoning"])
            self.history.append(message)
            tool_calls = message.get("tool_calls")
            if not tool_calls:
                return message.get("content") or ""
            for call in tool_calls:
                result = self._run_tool(call)
                self.history.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        return "I hit the tool-call limit for this request — let me know how you'd like to proceed."

    def _context(self) -> list[dict]:
        """The tail of history to send to the model: as much recent history as
        fits in MAX_CONTEXT_CHARS, trimmed to start on a user message so we
        never lead with an orphaned tool result or tool-call turn. Whatever
        falls before that point isn't just dropped (B2) -- it's replaced by a
        cached, incrementally-updated summary (see compact()), so nothing
        older is silently forgotten, only compressed."""
        start = self._drop_boundary()
        messages = self.history[start:]
        if start != 0:
            summary = self._compact(start)
            if summary:
                summary_msg = {
                    "role": "user",
                    "content": f"[Summary of {start} earlier messages, replaced here to stay within budget]\n{summary}",
                }
                messages = [summary_msg, *messages]
        canvas_note = self._pop_canvas_delta_summary()
        return [canvas_note, *messages] if canvas_note else messages

    def _pop_canvas_delta_summary(self) -> dict | None:
        """A one-off "here's what changed on the canvas since you last looked"
        message, built from hand-edits recorded by apply_canvas_edit/
        group_elements/ungroup_elements -- popped (cleared) once read, so it's
        sent exactly once, right before the turn that follows those edits."""
        if not self._pending_canvas_changes:
            return None
        text = "\n".join(self._pending_canvas_changes)
        if len(text) > MAX_CANVAS_DELTA_CHARS:
            text = text[:MAX_CANVAS_DELTA_CHARS] + " …"
        self._pending_canvas_changes = []
        return {"role": "user", "content": f"[Canvas changed since your last turn:]\n{text}"}

    def _drop_boundary(self) -> int:
        """Index of the first message that's RETAINED verbatim under the
        current char budget -- everything before this is what would be
        dropped (or, via compaction, summarized) rather than sent."""
        cutoff = self._budget_cutoff()
        user_turns = [i for i, m in enumerate(self.history) if m.get("role") == "user"]
        return next((i for i in user_turns if i >= cutoff), user_turns[-1] if user_turns else 0)

    def _context_preview(self) -> list[dict]:
        """Same shape as _context(), for READ-ONLY/informational use (the
        status line's context_len/context_chars) -- never triggers a fresh
        compaction call. _context() is the only method allowed to do that,
        since it's the real per-turn request path, already covered by the
        "thinking" spinner in interactive mode. This one only reuses a
        summary that's ALREADY cached; if the boundary has advanced past
        what's cached, it falls back to the plain (uncompacted) tail rather
        than block a passive read on a real network call -- a bug found
        live 2026-09-23: the status line re-evaluates this on every toolbar
        refresh, including the very first one before any message is sent,
        so a history that already exceeded the budget from a PRIOR session
        made simply opening R.A.V.E.N silently block for as long as a real
        OpenRouter call takes, with no spinner, before the prompt would
        accept any input. The tradeoff this accepts: the status line can
        show a stale (larger, pre-compaction) size until the next real ask()
        actually compacts -- cosmetic, and far better than blocking the UI."""
        start = self._drop_boundary()
        messages = self.history[start:]
        if start == 0 or start > self._compacted_through or not self._compacted_summary:
            return messages
        summary_msg = {
            "role": "user",
            "content": f"[Summary of {start} earlier messages, replaced here to stay within budget]\n{self._compacted_summary}",
        }
        return [summary_msg, *messages]

    def _compact(self, through: int) -> str | None:
        """Ensure self._compacted_summary covers self.history[:through],
        extending the cached summary incrementally if more history has fallen
        out of the budget window since the last compaction. Never mutates
        self.history or the Store -- only what _context() builds per request.
        Returns None (never raises) if there's no provider to ask or the call
        fails, so callers can fall back to the old plain-drop behavior rather
        than lose the turn."""
        if through <= self._compacted_through:
            return self._compacted_summary
        if self.provider is None:
            return None
        new_material = self.history[self._compacted_through:through]
        prompt = _build_compaction_prompt(self._compacted_summary, new_material)
        try:
            response = self.provider.reply([{"role": "user", "content": prompt}])
        except Exception:
            return self._compacted_summary  # keep whatever we had rather than lose it to a failed call
        summary = strip_emoji(response.get("content") or "") or self._compacted_summary
        if summary and len(summary) > MAX_SUMMARY_CHARS:
            summary = summary[:MAX_SUMMARY_CHARS] + f" … (truncated, {len(summary) - MAX_SUMMARY_CHARS} more chars)"
        self._compacted_summary = summary
        self._compacted_through = through
        return summary

    def compact(self) -> str:
        """Manually trigger compaction of whatever currently falls outside the
        context budget (/compact). Calls the same _compact() the automatic
        path uses lazily from _context(), just forced now instead of on the
        next request that would need it."""
        start = self._drop_boundary()
        if start == 0:
            return "Nothing to compact -- the whole conversation still fits in the context budget."
        if start <= self._compacted_through:
            return f"Already compacted through message {self._compacted_through} -- nothing new since then."
        summary = self._compact(start)
        if summary is None:
            return "Couldn't compact -- no model available to summarize with."
        return f"Compacted {start} earlier messages into a running summary ({len(summary)} chars)."

    def _budget_cutoff(self) -> int:
        """Index of the earliest message that still fits within
        MAX_CONTEXT_CHARS, scanning backward from the most recent message.
        Always includes at least the most recent message, however large —
        the caller (_context) falls back to the last user turn in that case,
        the same way it already did for the old message-count cap."""
        remaining = MAX_CONTEXT_CHARS
        for i in range(len(self.history) - 1, -1, -1):
            remaining -= len(json.dumps(self.history[i]))
            if remaining < 0:
                return i + 1
        return 0

    @property
    def context_len(self) -> int:
        """Number of history messages currently sent to the model per
        request. Uses _context_preview(), not _context() -- see its
        docstring for why (must never trigger a real network call just to
        answer a status-line query)."""
        return len(self._context_preview())

    @property
    def context_chars(self) -> int:
        """Approximate size, in characters, of what's currently sent to the
        model per request (see MAX_CONTEXT_CHARS). Same _context_preview()
        caveat as context_len."""
        return sum(len(json.dumps(m)) for m in self._context_preview())

    def _persist(self) -> None:
        if self.store and len(self.history) > self._persisted:
            self.store.append(self.history[self._persisted:])
            self._persisted = len(self.history)

    def _active_tool_specs(self) -> list[dict]:
        """What's actually sent to the model this request (D2): core tools
        plus whatever skills have been loaded so far this session, plus
        load_skill itself (always offered, so there's always a way to ask
        for more)."""
        active_names = tool_impl.core_tool_names() | {
            t for skill in self.active_skills for t in tool_impl.SKILLS[skill]
        }
        return [LOAD_SKILL_SPEC] + [s for s in tool_impl.TOOL_SPECS if s["function"]["name"] in active_names]

    def _load_skill(self, name: str) -> str:
        """Unlock a skill's tools for the rest of this session, and (if the
        provider has one) extend its live system_prompt with that skill's
        guardrail instructions -- mutated in place, so every subsequent
        reply() call picks it up automatically with no other plumbing
        needed. getattr(..., None) guards this for BOTH cases where there's
        nothing to append to: a duck-typed fake/test provider with no
        system_prompt attribute at all (returns None, the default), and a
        REAL LLMProvider subclass that just never set it to an actual
        string (also None -- LLMProvider itself declares it with a None
        default, purely so the attribute is validly TYPED on the ABC; it's
        still None on any subclass that doesn't override it, so checking
        `is not None` here, not just attribute existence, is required --
        `hasattr` alone would be True in that second case and crash trying
        to += a string onto None. The tools still unlock either way in both
        cases, which is the larger of the two benefits."""
        if name not in tool_impl.SKILLS:
            return f"Unknown skill: {name}. Available: {', '.join(tool_impl.SKILLS)}."
        if name in self.active_skills:
            return f"Skill '{name}' is already loaded."
        self.active_skills.add(name)
        addition = SKILL_PROMPTS.get(name, "")
        current_prompt = getattr(self.provider, "system_prompt", None)
        if addition and current_prompt is not None:
            self.provider.system_prompt = current_prompt + addition
        return f"Loaded skill '{name}': {', '.join(tool_impl.SKILLS[name])}"

    def _analyze_image(self, path: str, question: str) -> str:
        """Handles analyze_image (V2) -- intercepted in _run_tool before the
        generic dispatch, same reason as load_skill: it needs self.provider
        to make a real vision-model call, which a plain tool_impl.py
        function has no access to. Read-only, never confirmed, same policy
        as read_file/browser_read. Never raises -- every failure mode
        (unconfigured model, unsupported provider, unreadable file, a failed
        API call) is reported as a normal "Error: ..." string, consistent
        with every other tool's error convention in this project."""
        if not self.vision_model:
            return "Error: no vision model configured (settings.json's model.vision)."
        if not hasattr(self.provider, "reply_with_image"):
            return "Error: this provider doesn't support image analysis."
        try:
            image_bytes = Path(path).expanduser().resolve().read_bytes()
        except OSError as e:
            return f"Error: couldn't read {path}: {e}"
        mime_type = mimetypes.guess_type(path)[0] or "image/png"
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        try:
            return self.provider.reply_with_image(question, image_b64, mime_type, self.vision_model)
        except Exception as e:
            return f"Error: {e}"

    # -- Design mode: mathematically-generated shapes on the session's canvas ---
    #
    # Every element lives in self.canvas (a flat list -- a "group" element's
    # children are nested INSIDE it, not separate top-level entries, so
    # list/update/delete_canvas_element only ever address a top-level shape
    # or a whole group at once, not one shape inside a group). Geometry is
    # always DERIVED from "params" via raven/design.py's generators -- never
    # hand-patched -- so editing params later regenerates exact geometry
    # instead of drifting into an approximation. None of these tools confirm:
    # they only ever mutate this session's own saved canvas, nothing external
    # or hard to reverse (delete is trivially undoable -- redraw, or the
    # UI's own undo), the same tier read-only/session-state tools already get.

    _DESIGN_GENERATOR_FOR_TOOL = {
        "draw_golden_rectangle": "golden_rectangle",
        "draw_golden_spiral": "golden_spiral",
        "draw_polygon": "regular_polygon",
        "draw_star": "star_polygon",
        "draw_grid": "grid_layout",
        "draw_bezier": "bezier_curve",
        "draw_lissajous": "lissajous_curve",
    }
    _PLACEMENT_ARG_KEYS = ("x", "y", "rotation", "fill", "stroke", "stroke_width")

    def _run_design_tool(self, name: str, args: dict) -> str:
        # Real bug, found live: several models (confirmed with a real
        # NVIDIA-hosted model) send numeric tool arguments as JSON STRINGS
        # ("80" instead of 80) even though the tool spec declares them
        # "number"/"integer" -- the schema is only a hint to the model, never
        # enforced by anything on the wire. Without this, every draw/update
        # call from such a model crashed immediately (design.py's math does
        # `radius * cos(...)`, and `"80" * cos(...)` raises a TypeError) --
        # coercing here, once, at the tool boundary, means every generator
        # and _apply_patch can keep trusting its inputs are real numbers.
        args = _coerce_numeric_strings(args)
        if name == "list_canvas_elements":
            return self._list_canvas_elements()
        if name == "update_canvas_element":
            return self._update_canvas_element(args.get("element_id", ""), args.get("patch") or {})
        if name == "delete_canvas_element":
            return self._delete_canvas_element(args.get("element_id", ""))
        if name == "draw_radial_pattern":
            return self._draw_radial_pattern(args)
        generator_name = self._DESIGN_GENERATOR_FOR_TOOL.get(name)
        if generator_name is None:
            return f"Unknown design tool: {name}"
        return self._draw(generator_name, args)

    def _draw(self, generator_name: str, args: dict) -> str:
        gen_kwargs = {k: v for k, v in args.items() if k not in self._PLACEMENT_ARG_KEYS}
        try:
            geometry = design_impl.regenerate(generator_name, gen_kwargs)
        except (ValueError, TypeError) as e:
            return f"Error: {e}"
        el = self._place_and_add(geometry, generator_name, args)
        return self._describe_element(el, verb="Drew")

    def _draw_radial_pattern(self, args: dict) -> str:
        motif_name = args.get("motif")
        motif_params = args.get("motif_params") or {}
        if motif_name not in design_impl.GENERATORS:
            return f"Error: unknown motif '{motif_name}' — use one of {sorted(design_impl.GENERATORS)}."
        try:
            motif = design_impl.GENERATORS[motif_name](**motif_params)
            geometry = design_impl.radial_pattern(
                motif, repetitions=args.get("repetitions", 6), orbit_radius=args.get("orbit_radius", 0.0),
                spin_with_orbit=args.get("spin_with_orbit", True),
            )
        except (ValueError, TypeError) as e:
            return f"Error: {e}"
        el = self._place_and_add(geometry, "radial_pattern", args)
        return self._describe_element(el, verb="Drew")

    def _place_and_add(self, geometry: dict, generator_name: str, args: dict) -> dict:
        el = design_impl.place_element(
            geometry,
            x=args.get("x", 0.0), y=args.get("y", 0.0), rotation=args.get("rotation", 0.0),
            fill=args.get("fill", "#141414"), stroke=args.get("stroke", "none"),
            stroke_width=args.get("stroke_width", 1.0), z=self._next_z(),
        )
        el["generator"] = generator_name
        _ensure_visible(el)
        self.canvas.append(el)
        self._persist_canvas()
        self.on_canvas_update(el)
        return el

    def _regenerate_element_geometry(self, el: dict) -> dict:
        """Rebuilds an element's geometry from its (possibly just-edited)
        params, by calling the exact generator that first created it.
        radial_pattern is the one special case -- its own generator takes an
        already-built motif ELEMENT, not flat params, so its motif has to be
        rebuilt first from the motif params radial_pattern's own return
        value already stores (see design.radial_pattern's "params")."""
        generator_name = el.get("generator")
        if generator_name == "radial_pattern":
            p = el["params"]
            motif = design_impl.GENERATORS[p["motif_type"]](**p["motif_params"])
            return design_impl.radial_pattern(
                motif, repetitions=p["repetitions"], orbit_radius=p["orbit_radius"],
                spin_with_orbit=p["spin_with_orbit"],
            )
        return design_impl.regenerate(generator_name, el["params"])

    def _find_element(self, element_id: str) -> dict | None:
        return next((el for el in self.canvas if el["id"] == element_id), None)

    def _next_z(self) -> int:
        return max((el.get("z", 0) for el in self.canvas), default=-1) + 1

    def _element_summary(self, el: dict) -> str:
        kind = (el.get("generator") or el.get("type", "shape")).replace("_", " ")
        return f"{kind} '{el['id']}' at ({el['x']:.0f}, {el['y']:.0f}), rotation {el['rotation']:.0f}"

    def _describe_element(self, el: dict, verb: str) -> str:
        return f"{verb} {self._element_summary(el)}."

    def _list_canvas_elements(self) -> str:
        if not self.canvas:
            return "The canvas is empty."
        lines = [
            f"- {el['id']}: {el.get('generator', el['type'])} at ({el['x']:.0f}, {el['y']:.0f}), "
            f"rotation {el['rotation']:.0f}, z={el['z']}, fill={el['fill']}, stroke={el['stroke']}, "
            f"params={json.dumps(el.get('params', {}))}"
            for el in sorted(self.canvas, key=lambda e: e["z"])
        ]
        return "Canvas elements (top to bottom is highest to lowest z):\n" + "\n".join(lines)

    def _apply_patch(self, element_id: str, patch: dict) -> dict | None:
        """Applies `patch` to an existing element -- a "params" key merges
        into the element's existing params and regenerates geometry from the
        result (never trusts client/model-sent geometry directly); any of
        x/y/rotation/scale/fill/stroke/stroke_width/z just overwrite that
        field. Shared by _update_canvas_element (a model tool call) and
        apply_canvas_edit (a direct hand-edit from the UI, no model
        involved) so both go through exactly the same regeneration logic."""
        patch = _coerce_numeric_strings(patch)  # same defensive coercion _run_design_tool does
        el = self._find_element(element_id)
        if el is None:
            return None
        if "params" in patch:
            el["params"] = {**el.get("params", {}), **patch["params"]}
            geometry = self._regenerate_element_geometry(el)
            for key in ("points", "d", "children"):
                el.pop(key, None)
                if key in geometry:
                    el[key] = geometry[key]
            el["params"] = geometry.get("params", el["params"])
        for key in ("x", "y", "rotation", "scale", "fill", "stroke", "stroke_width", "z"):
            if key in patch:
                el[key] = patch[key]
        if "fill" in patch or "stroke" in patch:
            _ensure_visible(el)  # same invisible-shape guard _place_and_add applies on creation
        self._persist_canvas()
        return el

    def _update_canvas_element(self, element_id: str, patch: dict) -> str:
        try:
            el = self._apply_patch(element_id, patch)
        except (ValueError, TypeError, KeyError) as e:
            return f"Error: couldn't apply those changes: {e}"
        if el is None:
            return f"Error: no element with id '{element_id}'. Call list_canvas_elements first."
        self.on_canvas_update(el)
        return self._describe_element(el, verb="Updated")

    def apply_canvas_edit(self, element_id: str, patch: dict) -> dict | None:
        """The user's own hand-edit (drag/resize/rotate/recolor/reparametrize/
        reorder) made directly in the UI -- server.py calls this straight
        from the websocket handler, bypassing the model entirely (a drag
        doesn't need an LLM round trip). Recorded as a pending delta (see
        _pop_canvas_delta_summary) since, unlike a model-driven
        update_canvas_element call, R.A.V.E.N did not just do this herself
        and has no other way to find out before her next turn."""
        try:
            el = self._apply_patch(element_id, patch)
        except (ValueError, TypeError, KeyError):
            return None
        if el is not None:
            self._pending_canvas_changes.append(f"User edited {self._element_summary(el)}.")
            self.on_canvas_update(el)
        return el

    def _delete_canvas_element(self, element_id: str) -> str:
        el = self._find_element(element_id)
        if el is None:
            return f"Error: no element with id '{element_id}'. Call list_canvas_elements first."
        self.canvas.remove(el)
        self._persist_canvas()
        self.on_canvas_delete(element_id)
        return f"Deleted {(el.get('generator') or el['type']).replace('_', ' ')} '{element_id}'."

    def delete_canvas_element_by_user(self, element_id: str) -> bool:
        """Same as _delete_canvas_element, but from the UI (server.py) rather
        than a model tool call -- recorded as a pending delta for the same
        reason apply_canvas_edit is."""
        el = self._find_element(element_id)
        if el is None:
            return False
        self.canvas.remove(el)
        self._persist_canvas()
        self._pending_canvas_changes.append(f"User deleted {self._element_summary(el)}.")
        self.on_canvas_delete(element_id)
        return True

    def group_elements(self, element_ids: list[str]) -> dict | None:
        """Groups existing top-level canvas elements into one new group
        element, positioned at their centroid with an identity rotation/
        scale -- each child's own x/y is re-expressed relative to that new
        origin, so the group starts out visually identical to before
        grouping (decision: a group carries its OWN transform layered over
        each child's still-independent params, not baked-in absolute
        positions -- see ungroup_elements for the inverse)."""
        els = [el for el in self.canvas if el["id"] in element_ids]
        if len(els) < 2:
            return None
        cx = sum(el["x"] for el in els) / len(els)
        cy = sum(el["y"] for el in els) / len(els)
        children = []
        for el in els:
            child = dict(el)
            child["x"] = el["x"] - cx
            child["y"] = el["y"] - cy
            children.append(child)
            self.canvas.remove(el)
        group = design_impl.place_element(
            {"type": "group", "children": children, "params": {}},
            x=cx, y=cy, rotation=0.0, fill="none", stroke="none", z=self._next_z(),
        )
        self.canvas.append(group)
        self._persist_canvas()
        self._pending_canvas_changes.append(f"User grouped {len(children)} shapes into '{group['id']}'.")
        self.on_canvas_update(group)
        return group

    def ungroup_elements(self, group_id: str) -> list[dict] | None:
        """The inverse of group_elements: bakes the group's own x/y/rotation/
        scale onto each child (standard rotate-then-scale-then-translate
        composition, matching how the frontend renders a group as a nested
        SVG <g transform=...>), so every child ends up exactly where it
        visually was, as an independent top-level element again."""
        group = self._find_element(group_id)
        if group is None or group.get("type") != "group":
            return None
        theta = math.radians(group.get("rotation", 0.0))
        scale = group.get("scale", 1.0)
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        restored = []
        for child in group.get("children", []):
            el = dict(child)
            rx = child["x"] * cos_t - child["y"] * sin_t
            ry = child["x"] * sin_t + child["y"] * cos_t
            el["x"] = group["x"] + rx * scale
            el["y"] = group["y"] + ry * scale
            el["rotation"] = child.get("rotation", 0.0) + group.get("rotation", 0.0)
            el["scale"] = child.get("scale", 1.0) * scale
            el["z"] = self._next_z()
            self.canvas.append(el)
            restored.append(el)
        self.canvas.remove(group)
        self._persist_canvas()
        self._pending_canvas_changes.append(f"User ungrouped '{group_id}' into {len(restored)} shapes.")
        for el in restored:
            self.on_canvas_update(el)
        self.on_canvas_delete(group_id)
        return restored

    def restore_element(self, el: dict) -> dict:
        """Puts a previously-deleted element back exactly as it was (same
        id) -- the undo half of delete_canvas_element_by_user, from the
        UI's own undo stack (the client keeps the full element it just
        asked to delete, so undo can hand it straight back rather than
        needing the server to have remembered it)."""
        self.canvas.append(el)
        self._persist_canvas()
        self.on_canvas_update(el)
        return el

    def _persist_canvas(self) -> None:
        if self.store:
            self.store.save_canvas(self.canvas)

    def forget(self) -> None:
        """Clear conversation history, in memory and on disk. Stale tool results
        (e.g. "app X isn't installed") can otherwise sit in context indefinitely
        and get treated as still true even after the real state has changed."""
        self.history.clear()
        self._persisted = 0
        if self.store:
            self.store.clear()

    @staticmethod
    def _is_safe_command(command: str, allowed: frozenset[str] = frozenset()) -> bool:
        """True only for a single, argument-only invocation of a read-only
        program — no shell metacharacters, so an unsafe command can't hide
        behind a safe-looking first word (e.g. "ls; rm -rf ~"). `allowed`
        (C3, from settings.json's permissions.allow_commands) is checked in
        ADDITION to the hardcoded SAFE_COMMANDS, never in place of it — and
        the metacharacter check above always applies regardless of what's
        in `allowed`, so a user-trusted program name never becomes a way to
        smuggle in chaining/redirection/substitution."""
        command = command.strip()
        if not command:
            return False
        # A pipeline of read-only programs (e.g. `pdftotext f.pdf - | head -50`)
        # is as safe as its parts; ANY unsafe segment, or any unquoted
        # chaining/redirection/substitution, makes the whole command need confirmation.
        segments = _split_safe_pipeline(command)
        if segments is None:
            return False
        return all(_segment_is_safe(seg, allowed) for seg in segments)

    @staticmethod
    def _confirm_prompt(
        name: str, args: dict, allowed_commands: frozenset[str] = frozenset(),
        allowed_git: frozenset[str] = frozenset(),
    ) -> str | None:
        """A human-readable action string if this call needs confirmation,
        else None. allowed_commands/allowed_git (C3) extend, never replace,
        the hardcoded SAFE_COMMANDS/SAFE_GIT — see _is_safe_command."""
        if name in ("run_command", "run_tests"):
            # run_tests defaults to "pytest" when the model omits `command`
            # (same default tools.run_tests itself uses) -- without mirroring
            # that default here, an omitted arg would show a blank, confusing
            # "run: " prompt instead of what's actually about to execute.
            command = args.get("command") or ("pytest" if name == "run_tests" else "")
            return None if Assistant._is_safe_command(command, allowed_commands) else f"run: {command}"
        if name == "delete_file":
            return f"delete file: {args.get('path', '')}"
        if name == "write_file":
            # Only overwriting an EXISTING file is consequential — creating a
            # new one isn't, same policy as everything else in this project
            # (open_app vs close_app, browser_click vs browser_submit).
            path = args.get("path", "")
            if not tool_impl._is_existing_file(path):
                return None
            content = args.get("content", "")
            preview = content if len(content) <= 300 else content[:300] + " …"
            return f"overwrite {path}\n  New content: {preview}"
        if name == "edit_file":
            # Unlike write_file, edit_file only ever touches a file that
            # already exists — there's no "creating something new" case to
            # exempt, so this always confirms, same tier as write_file's
            # overwrite branch.
            old = args.get("old_string", "")
            new = args.get("new_string", "")
            old_preview = old if len(old) <= 300 else old[:300] + " …"
            new_preview = new if len(new) <= 300 else new[:300] + " …"
            return f"edit {args.get('path', '')}\n  Replace: {old_preview}\n  With:    {new_preview}"
        if name == "close_app":
            # close_app works by process signal, so it closes EVERY window of
            # the app at once. Say so up front rather than surprise the user;
            # close_window (below) is the real per-window alternative.
            return f"close ALL windows of {args.get('name', '')}"
        if name == "close_window":
            return f"close window: {args.get('title', '')}"
        if name == "browser_submit":
            return f"submit in browser: {args.get('text', '')}"
        if name == "gmail_send_message":
            body = args.get("body", "")
            preview = body if len(body) <= 300 else body[:300] + " …"
            return (f"send email\n  To: {args.get('to', '')}\n"
                    f"  Subject: {args.get('subject', '')}\n  Body: {preview}")
        if name == "gmail_reply_message":
            # The recipient isn't in the arguments (only a message id is), so the
            # preview looks it up with the SAME helper the send uses — that way
            # what's shown is exactly where the reply goes, including a Reply-To
            # that differs from the visible sender. A failed lookup still yields
            # a prompt (never None): a lookup error must not skip confirmation.
            body = args.get("body", "")
            preview = body if len(body) <= 300 else body[:300] + " …"
            return (f"send reply\n  {gmail_impl.reply_preview(args.get('message_id', ''))}\n"
                    f"  Body: {preview}")
        if name == "git":
            git_args = args.get("args") or "status"
            sub = git_args.split()[:1]
            if _has_shell_metachars(git_args) or not sub or (sub[0] not in SAFE_GIT and sub[0] not in allowed_git):
                return f"run: git {git_args}"
        return None

    def _run_hook(self, stage: str, tool_name: str, args: dict) -> str | None:
        """Run the user-configured `stage` ("pre" or "post") hook for
        `tool_name`, if any (settings.json's hooks section, D1). The command
        template's {placeholders} are filled from the tool call's own
        arguments (e.g. "black {path}" for edit_file) -- a template
        referencing an argument the call didn't provide is reported as an
        error string, never raised, same as everything else in this method.
        Never confirmed: being listed in settings.json IS the approval."""
        template = self.hooks.get(stage, {}).get(tool_name)
        if not template:
            return None
        try:
            command = template.format(**args)
        except (KeyError, IndexError) as e:
            return f"[{stage}-hook error] missing placeholder {e} in \"{template}\""
        try:
            return f"[{stage}-hook] {tool_impl.run_command(command)}"
        except Exception as e:
            return f"[{stage}-hook error] {e}"

    def _run_tool(self, call: dict) -> str:
        name = call["function"]["name"]
        args = json.loads(call["function"]["arguments"] or "{}")
        self.on_tool_call(name, args)
        if name == "load_skill":
            # Meta/Assistant-instance state, not a tool_impl.py "operate on
            # the world" primitive -- handled here, before the generic
            # confirmation/hook/dispatch machinery below, none of which
            # applies to it (never confirmed, no hooks, no TOOL_FUNCTIONS entry).
            return self._load_skill(args.get("name", ""))
        if name == "analyze_image":
            # Needs self.provider for a real vision-model call -- tools.py's
            # analyze_image is only a placeholder (see its docstring), same
            # interception shape as load_skill above, but this one DOES flow
            # through the normal skill-gating/registry (tools.SKILLS/TOOL_SPECS)
            # since it's a real, skill-gated tool from the model's point of
            # view, just not a plain tool_impl.py function underneath.
            return self._analyze_image(args.get("path", ""), args.get("question") or "Describe this image.")
        if name in tool_impl.SKILLS.get("design", ()):
            # Same interception shape again -- every design tool needs live
            # access to self.canvas (list/create/edit/delete a shape), which
            # a stateless tool_impl.py function has no way to reach. Never
            # confirmed (see _run_design_tool's own module-level note): these
            # only ever touch this session's own canvas, nothing external.
            return self._run_design_tool(name, args)
        prompt = self._confirm_prompt(name, args, self.allowed_commands, self.allowed_git)
        if prompt:
            plat.current().notify("R.A.V.E.N needs confirmation", prompt)
            if not self.confirm_run(prompt):
                return "Cancelled by user."
        func = tool_impl.TOOL_FUNCTIONS.get(name)
        if not func:
            return f"Unknown tool: {name}"
        pre_output = self._run_hook("pre", name, args)
        try:
            result = str(func(**args))
        except Exception as e:
            return f"Error: {e}"
        # A post hook only makes sense after the tool call actually did
        # something -- not after edit_file's own "Error: ..." refusal (not
        # found / ambiguous match), which is a no-op, not a change.
        post_output = None if result.startswith("Error:") else self._run_hook("post", name, args)
        extra = "".join(f"\n{o}" for o in (pre_output, post_output) if o)
        return result + extra
