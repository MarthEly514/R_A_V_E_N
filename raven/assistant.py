"""Conversation state and tool-calling orchestration."""
import json
import re
import time
from typing import Callable

from raven.llm_provider import LLMProvider, SKILL_PROMPTS
from raven.store import Store
from raven import gmail as gmail_impl
from raven import tools as tool_impl

MAX_TOOL_ITERATIONS = 10  # was 5; browser workflows (navigate/read/act/verify) routinely need more
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
        settings: dict | None = None,
    ):
        self.provider = provider
        self.confirm_run = confirm_run
        self.store = store
        self.on_tool_call = on_tool_call  # notified (name, args) right before each tool runs
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
        self.history: list[dict] = store.load() if store else []
        self._persisted = len(self.history)
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
        if start == 0:
            return messages
        summary = self._compact(start)
        if not summary:
            return messages  # no provider to summarize with, or the call failed -- fall back to a plain drop
        summary_msg = {
            "role": "user",
            "content": f"[Summary of {start} earlier messages, replaced here to stay within budget]\n{summary}",
        }
        return [summary_msg, *messages]

    def _drop_boundary(self) -> int:
        """Index of the first message that's RETAINED verbatim under the
        current char budget -- everything before this is what would be
        dropped (or, via compaction, summarized) rather than sent."""
        cutoff = self._budget_cutoff()
        user_turns = [i for i, m in enumerate(self.history) if m.get("role") == "user"]
        return next((i for i in user_turns if i >= cutoff), user_turns[-1] if user_turns else 0)

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
        """Number of history messages currently sent to the model per request."""
        return len(self._context())

    @property
    def context_chars(self) -> int:
        """Approximate size, in characters, of what's currently sent to the
        model per request (see MAX_CONTEXT_CHARS)."""
        return sum(len(json.dumps(m)) for m in self._context())

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
        needed. hasattr guards this for a fake/test provider with no
        system_prompt attribute at all: the tools still unlock either way,
        which is the larger of the two benefits."""
        if name not in tool_impl.SKILLS:
            return f"Unknown skill: {name}. Available: {', '.join(tool_impl.SKILLS)}."
        if name in self.active_skills:
            return f"Skill '{name}' is already loaded."
        self.active_skills.add(name)
        addition = SKILL_PROMPTS.get(name, "")
        if addition and hasattr(self.provider, "system_prompt"):
            self.provider.system_prompt += addition
        return f"Loaded skill '{name}': {', '.join(tool_impl.SKILLS[name])}"

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
        if not command or _has_shell_metachars(command):
            return False
        program = command.split()[0].rsplit("/", 1)[-1]  # strip any path prefix
        return program in SAFE_COMMANDS or program in allowed

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
        prompt = self._confirm_prompt(name, args, self.allowed_commands, self.allowed_git)
        if prompt and not self.confirm_run(prompt):
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
