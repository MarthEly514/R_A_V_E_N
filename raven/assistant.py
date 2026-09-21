"""Conversation state and tool-calling orchestration."""
import json
import re
import time
from typing import Callable

from raven.llm_provider import LLMProvider
from raven.store import Store
from raven import gmail as gmail_impl
from raven import tools as tool_impl

MAX_TOOL_ITERATIONS = 10  # was 5; browser workflows (navigate/read/act/verify) routinely need more
MAX_CONTEXT_MESSAGES = 40  # how much history to send to the model per request
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


class Assistant:
    def __init__(
        self,
        provider: LLMProvider,
        confirm_run: Callable[[str], bool] = lambda cmd: True,
        store: Store | None = None,
        on_tool_call: Callable[[str, dict], None] = lambda name, args: None,
    ):
        self.provider = provider
        self.confirm_run = confirm_run
        self.store = store
        self.on_tool_call = on_tool_call  # notified (name, args) right before each tool runs
        self.history: list[dict] = store.load() if store else []
        self._persisted = len(self.history)
        self.last_elapsed = 0.0  # wall time of the last completed ask(), in seconds
        self.last_reasoning: list[str] = []  # each call's reasoning trace, if the model gave one

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
            message = self.provider.reply(self._context(), tools=tool_impl.TOOL_SPECS)
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
        """The tail of history to send to the model: roughly the last
        MAX_CONTEXT_MESSAGES messages, trimmed to start on a user message so we
        never lead with an orphaned tool result or tool-call turn."""
        cutoff = len(self.history) - MAX_CONTEXT_MESSAGES
        user_turns = [i for i, m in enumerate(self.history) if m.get("role") == "user"]
        start = next((i for i in user_turns if i >= cutoff), user_turns[-1] if user_turns else 0)
        return self.history[start:]

    @property
    def context_len(self) -> int:
        """Number of history messages currently sent to the model per request."""
        return len(self._context())

    def _persist(self) -> None:
        if self.store and len(self.history) > self._persisted:
            self.store.append(self.history[self._persisted:])
            self._persisted = len(self.history)

    def forget(self) -> None:
        """Clear conversation history, in memory and on disk. Stale tool results
        (e.g. "app X isn't installed") can otherwise sit in context indefinitely
        and get treated as still true even after the real state has changed."""
        self.history.clear()
        self._persisted = 0
        if self.store:
            self.store.clear()

    @staticmethod
    def _is_safe_command(command: str) -> bool:
        """True only for a single, argument-only invocation of an allow-listed
        read-only program — no shell metacharacters, so an unsafe command can't
        hide behind a safe-looking first word (e.g. "ls; rm -rf ~")."""
        command = command.strip()
        if not command or _has_shell_metachars(command):
            return False
        program = command.split()[0].rsplit("/", 1)[-1]  # strip any path prefix
        return program in SAFE_COMMANDS

    @staticmethod
    def _confirm_prompt(name: str, args: dict) -> str | None:
        """A human-readable action string if this call needs confirmation, else None."""
        if name == "run_command":
            command = args.get("command", "")
            return None if Assistant._is_safe_command(command) else f"run: {command}"
        if name == "delete_file":
            return f"delete file: {args.get('path', '')}"
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
            if _has_shell_metachars(git_args) or not sub or sub[0] not in SAFE_GIT:
                return f"run: git {git_args}"
        return None

    def _run_tool(self, call: dict) -> str:
        name = call["function"]["name"]
        args = json.loads(call["function"]["arguments"] or "{}")
        self.on_tool_call(name, args)
        prompt = self._confirm_prompt(name, args)
        if prompt and not self.confirm_run(prompt):
            return "Cancelled by user."
        func = tool_impl.TOOL_FUNCTIONS.get(name)
        if not func:
            return f"Unknown tool: {name}"
        try:
            return str(func(**args))
        except Exception as e:
            return f"Error: {e}"
