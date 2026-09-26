"""CLI entry point for R.A.V.E.N."""
import argparse

from pathlib import Path

import psutil
import threading
from rich.console import Console
from rich.markdown import Markdown
from rich.status import Status
from rich._spinners import SPINNERS
from rich.table import Table
from rich.prompt import Confirm
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

from raven.config import get_api_key, load_settings, save_settings, SETTINGS_PATH, NOTES_PATH
from raven.llm_provider import OpenRouterProvider, load_project_rules
from raven.assistant import Assistant
from raven.store import Store
from raven import tools, statusline, voice
from random import randint

SETTINGS: dict = {}  # populated in main(); /statusline edits it live
UI_STATE = {"show_thoughts": False, "recording": False, "voice_status": ""}  # ctrl+o/ctrl+v toggles
VOICE_RECORDER = voice.Recorder()  # cheap to construct; no mic opened until .start()
ASSISTANT: Assistant | None = None  # populated in main(); /forget clears its history

# primary_color = "#C9A3FF"
primary_color = "white"
user = "Ely514"

console = Console()

PROMPT_STYLE = Style.from_dict({
    "completion-menu.completion": "fg:#000000",
    "completion-menu.completion.current": f"bg:#000000 fg:{primary_color} bold",
})

STARTUP_SENTENCES = [
    f"Good morning {user}. Systems are ready. What are we working on today?",
    "Welcome back. I'm ready to assist. What is our first task?",
    "Hello. File and shell tools are initialized. How can I help you today?",
    "Online and ready. Let me know what you need to accomplish.",
]

THINKING_WORDS = [
    "Working on it...",
    "Thinking...",
    "Ravening...",
    "Swimming...",
    "Calculating...",
    "Casting...",
    "Mogging...",
    "Smoking...",
    "Pasting...",
    "Burning...", 
    "Evaporating...",
    "Hallucinating...",
    "Demotivating...",
    "Catastrophing...",
    "Ctrl+C-ing...",
    "Ctrl+V-ing...",
    "Whispering...",
    "Traveling...",
    "Cooking...",
    "Eating...",
    "Combusting...",
    "Vibe coding...",
    "Crushing...",
    
]

# A small custom Rich spinner (registered into rich.spinner.SPINNERS, the
# same dict Rich's own built-ins live in) so the thinking status shows a
# distinct animated glyph rather than the generic default. Plain rounded-arc
# unicode, not an emoji — matches the assistant's own no-emoji, calm-and-
# professional tone (that rule is about the model's OWN text; this is just a
# stylistic choice for consistency, not an enforcement of it).
# SPINNERS["raven"] = {"interval": 120, "frames": ["◜", "◠", "◝", "◞", "◡", "◟"]}
# SPINNERS["dots3"]

# Shimmer effect (esthetic patch, 2026-09-23): a brightness peak sweeps left
# to right across the thinking word, looping. Purely a rendering helper —
# takes a word and a frame counter, returns Rich markup; has no timing state
# of its own so it's trivially testable frame by frame.
_SHIMMER_STYLES = ["grey42", "grey58", "grey74", "white", "bold white", "grey74", "grey58", "grey42"]


def shimmer_text(word: str, frame: int) -> str:
    """Render `word` with a bright highlight sweeping across it, `frame`
    steps in — a window of _SHIMMER_STYLES centered on a position that
    advances one character per frame and wraps once it's swept past the
    whole word (word length + gradient width, so the shine fully exits
    before looping back to the start)."""
    if not word:
        return ""
    peak = frame % (len(word) + len(_SHIMMER_STYLES))
    out = []
    for i, ch in enumerate(word):
        offset = peak - i
        style = _SHIMMER_STYLES[offset] if 0 <= offset < len(_SHIMMER_STYLES) else "dim"
        out.append(f"[{style}]{ch}[/{style}]")
    return "".join(out)

# What to show in the status line while a given tool is running. Add an entry
# here whenever a new tool is added to tools.py (e.g. a future web-search tool).
TOOL_STATUS = {
    "read_file": lambda a: f"Reading {a.get('path', 'a file')}...",
    "write_file": lambda a: f"Writing {a.get('path', 'a file')}...",
    "make_dir": lambda a: f"Creating directory {a.get('path', '')}...",
    "save_note": lambda a: "Saving a note...",
    "edit_file": lambda a: f"Editing {a.get('path', 'a file')}...",
    "glob_files": lambda a: f"Finding files matching \"{a.get('pattern', '')}\"...",
    "tree": lambda a: f"Mapping {a.get('path', 'the current directory')}...",
    "list_dir": lambda a: f"Looking into {a['path']}..." if a.get("path", ".") not in ("", ".")
                           else "Looking into the current directory...",
    "grep": lambda a: f"Searching for \"{a.get('pattern', '')}\"...",
    "delete_file": lambda a: f"Deleting {a.get('path', 'a file')}...",
    "run_command": lambda a: f"Running: {a.get('command', 'a command')}...",
    "run_tests": lambda a: f"Running tests: {a.get('command', 'pytest')}...",
    "git": lambda a: f"Running git {a.get('args', 'status')}...",
    "open_app": lambda a: f"Opening {a.get('name', 'an app')}...",
    "close_app": lambda a: f"Closing {a.get('name', 'an app')}...",
    "list_windows": lambda a: "Checking open windows...",
    "close_window": lambda a: f"Closing window \"{a.get('title', '')}\"...",
    "open_file": lambda a: f"Opening {a.get('path', 'a file')}"
                            + (f" in {a['app']}..." if a.get("app") else "..."),
    "list_apps": lambda a: "Checking installed applications...",
    "open_url": lambda a: f"Opening {a.get('url', 'a page')}"
                           + (f" in {a['app']}..." if a.get("app") else " in the browser..."),
    "fetch_url": lambda a: f"Reading {a.get('url', 'a page')}...",
    "web_search": lambda a: f"Searching the web for \"{a.get('query', '')}\"...",
    "browser_navigate": lambda a: f"Loading {a.get('url', 'a page')} in the browser...",
    "browser_read": lambda a: "Reading the page...",
    "browser_click": lambda a: f"Clicking \"{a.get('text', '')}\"...",
    "browser_type": lambda a: f"Typing into \"{a.get('text', '')}\"...",
    "browser_submit": lambda a: f"Submitting \"{a.get('text', '')}\"...",
    "browser_close": lambda a: "Closing the browser session...",
    "browser_screenshot": lambda a: "Taking a screenshot...",
    "analyze_image": lambda a: f"Looking at {a.get('path', 'the image')}...",
    "gmail_list_messages": lambda a: "Checking Gmail...",
    "gmail_read_message": lambda a: "Reading an email...",
    "gmail_send_message": lambda a: f"Sending an email to {a.get('to', '')}...",
    "gmail_reply_message": lambda a: "Preparing a reply...",
    "load_skill": lambda a: f"Loading skill: {a.get('name', '')}...",
}


def tool_status_text(name: str, args: dict) -> str:
    fn = TOOL_STATUS.get(name)
    return fn(args) if fn else f"Using {name}..."



def show_help(_args: str = ""):
    table = Table(title="R.A.V.E.N Commands")
    table.add_column("Command", style=primary_color)
    table.add_column("Description")
    table.add_row("/help", "Show this help message")
    table.add_row("/status", "Show system status")
    table.add_row("/read <path>", "Print a file's contents")
    table.add_row("/write <path> <content>", "Write content to a file")
    table.add_row("/mkdir <path>", "Create a directory")
    table.add_row("/run <command>", "Run a shell command (asks to confirm)")
    table.add_row("/statusline [segments]", "Show or set the bottom status-line segments")
    table.add_row("/forget", "Clear conversation history (in memory and on disk)")
    table.add_row("/compact", "Summarize older history to free up context budget now")
    table.add_row("/notes", "Show saved cross-session notes (~/.raven/memory/notes.md)")
    table.add_row("/skills", "Show which skills are currently loaded this session")
    table.add_row("/exit", "Exit R.A.V.E.N")
    console.print(table)
    console.print()


def show_status(_args: str = ""):
    table = Table(title="R.A.V.E.N System Status")
    table.add_column("Component", style=primary_color)
    table.add_column("Status")
    table.add_row("CPU", f"{psutil.cpu_percent(interval=0.3):.1f}%")
    table.add_row("Memory", f"{psutil.virtual_memory().percent:.1f}%")
    table.add_row("Disk", f"{psutil.disk_usage(Path.cwd().anchor or '/').percent:.1f}%")
    console.print(table)
    console.print()


def handle_forget(_args: str = ""):
    assert ASSISTANT is not None
    count = len(ASSISTANT.history)
    ASSISTANT.forget()
    console.print(f"[green]✓ Forgot {count} messages. Starting fresh.[/green]")
    console.print()


def handle_notes(_args: str = ""):
    """Read-only view of ~/.raven/memory/notes.md — writes only happen via
    the model's save_note tool, deliberately (see Step 3.36); this is just
    visibility into what's currently loaded at startup."""
    if not NOTES_PATH.is_file() or not NOTES_PATH.read_text().strip():
        console.print(f"[dim]No notes saved yet ({NOTES_PATH}).[/]")
        console.print()
        return
    console.print(f"[dim]{NOTES_PATH}:[/]")
    console.print(NOTES_PATH.read_text().strip())
    console.print()


def handle_compact(_args: str = ""):
    assert ASSISTANT is not None
    with console.status("[dim]Compacting...[/]"):
        result = ASSISTANT.compact()
    style = "green" if result.startswith("Compacted") else "dim"
    console.print(f"[{style}]{'✓ ' if style == 'green' else ''}{result}[/{style}]")
    console.print()


def handle_skills(_args: str = ""):
    """Read-only view of which skills (D2) are currently loaded this
    session and what each one covers -- writes only happen via the model's
    own load_skill tool call, same read-only-visibility pattern as /notes
    and /statusline's no-args listing."""
    assert ASSISTANT is not None
    table = Table(title="Skills")
    table.add_column("Skill", style=primary_color)
    table.add_column("Description")
    table.add_column("Tools")
    for name, tool_names in tools.SKILLS.items():
        mark = "•" if name in ASSISTANT.active_skills else " "
        table.add_row(f"{mark} {name}", tools.SKILL_DESCRIPTIONS.get(name, ""), ", ".join(tool_names))
    console.print(table)
    console.print("[dim]Loaded by the model itself via load_skill when a task needs one -- "
                  "nothing to load manually. Core tools (always available) aren't listed here.[/]")
    console.print()


def handle_statusline(args: str):
    names = args.split()
    if not names:
        table = Table(title="Status line")
        table.add_column("Segment", style=primary_color)
        table.add_column("Shown")
        for name, fn in statusline.SEGMENTS.items():
            mark = "•" if name in SETTINGS["statusline"]["segments"] else " "
            table.add_row(f"{mark} {name}", fn.__doc__ or "")
        console.print(table)
        console.print(f"[dim]Order: {' '.join(SETTINGS['statusline']['segments'])}[/]")
        console.print(f"[dim]Edit with '/statusline <segment> <segment> ...'  (saved to {SETTINGS_PATH})[/]")
        console.print()
        return
    unknown = [n for n in names if n not in statusline.SEGMENTS]
    if unknown:
        console.print(f"[red]Unknown segment(s): {', '.join(unknown)}[/red]  "
                      f"[dim]choose from: {', '.join(statusline.SEGMENTS)}[/dim]")
        return
    SETTINGS["statusline"]["segments"] = names
    save_settings(SETTINGS)
    console.print(f"[green]✓ Status line: {'  •  '.join(names)}[/green]")


def handle_read(args: str):
    try:
        console.print(tools.read_file(args.strip()))
    except OSError as e:
        console.print(f"[red]Couldn't read file: {e}[/red]")


def handle_write(args: str):
    path, _, content = args.strip().partition(" ")
    if tools._is_existing_file(path) and not Confirm.ask(
        f"Overwrite existing file [bold]{path}[/]?", default=False
    ):
        console.print("[dim]Cancelled.[/dim]")
        return
    try:
        console.print(f"[green]✓ {tools.write_file(path, content)}[/green]")
    except OSError as e:
        console.print(f"[red]Couldn't write file: {e}[/red]")


def handle_mkdir(args: str):
    try:
        console.print(f"[green]✓ {tools.make_dir(args.strip())}[/green]")
    except OSError as e:
        console.print(f"[red]Couldn't create directory: {e}[/red]")


def handle_run(args: str):
    if not Confirm.ask(f"Run [bold]{args}[/]?", default=False):
        console.print("[dim]Cancelled.[/dim]")
        return
    console.print(tools.run_command(args))

def handle_exit(args: str=""):
    """A placeholder since the exit is managed in the main loop"""
    pass


COMMANDS = {
    "/help": show_help,
    "/status": show_status,
    "/read": handle_read,
    "/write": handle_write,
    "/mkdir": handle_mkdir,
    "/run": handle_run,
    "/statusline": handle_statusline,
    "/forget": handle_forget,
    "/compact": handle_compact,
    "/notes": handle_notes,
    "/skills": handle_skills,
    "/exit": handle_exit,
    "/quit": handle_exit,


}


KEY_BINDINGS = KeyBindings()


@KEY_BINDINGS.add("c-o")
def _toggle_thoughts(event):
    UI_STATE["show_thoughts"] = not UI_STATE["show_thoughts"]


def _run_transcription(audio, buffer, app) -> None:
    """Transcribe `audio` and insert the result into the input buffer.
    Runs on a BACKGROUND thread (see _toggle_recording) -- never on
    prompt_toolkit's event-loop thread, because loading the Whisper model
    and transcribing take seconds and used to freeze the whole UI
    (Ctrl+C and Ctrl+V included) while they ran. Results go back to the
    UI thread via call_soon_threadsafe, the only safe way to touch a
    prompt_toolkit buffer from another thread."""
    try:
        text = voice.transcribe(audio)
        error = ""
    except voice.ModelNotDownloadedError as e:
        text, error = "", str(e)
    except Exception as e:  # never let a voice failure kill the prompt
        text, error = "", f"voice transcription failed: {e}"

    def finish() -> None:
        UI_STATE["voice_status"] = error
        if text:
            buffer.insert_text(text)
        app.invalidate()

    app.loop.call_soon_threadsafe(finish)


@KEY_BINDINGS.add("c-v")
def _toggle_recording(event):
    """Push-to-talk, toggled (not held) -- ctrl+v starts recording, ctrl+v
    again stops it, transcribes locally (no network, no OpenRouter quota),
    and inserts the text into the current input buffer. Never auto-submits:
    the user reviews/edits/discards it like anything they'd typed, same
    posture as every other consequential action in this project.

    Nothing in here may block: this runs on the event-loop thread, so
    anything slow (model load, transcription) goes to a background thread,
    and nothing here may touch the network (voice.model_ready is a local
    disk check; the model is never downloaded from a keypress)."""
    if not voice.is_available():
        event.current_buffer.insert_text(
            "[voice input not set up — pip install sounddevice faster-whisper]"
        )
        return
    if UI_STATE["voice_status"] == "transcribing":
        return  # still working on the previous recording
    if not UI_STATE["recording"]:
        if not voice.model_ready():
            UI_STATE["voice_status"] = "voice model not downloaded — run: python -m raven.voice"
            event.app.invalidate()
            return
        UI_STATE["voice_status"] = ""
        UI_STATE["recording"] = True
        VOICE_RECORDER.start()
        return
    UI_STATE["recording"] = False
    audio = VOICE_RECORDER.stop()
    UI_STATE["voice_status"] = "transcribing"
    event.app.invalidate()  # show "transcribing" immediately, before the worker finishes
    threading.Thread(
        target=_run_transcription, args=(audio, event.current_buffer, event.app), daemon=True,
    ).start()


MAX_TOOLBAR_REASONING_CHARS = 1200  # keep the toolbar a status area, not an unbounded pager


def render_thought_line(elapsed: float, has_reasoning: bool) -> None:
    """Print the permanent scrollback record of how long a reply took. The
    reasoning trace itself lives in the bottom toolbar (see reasoning_toolbar_text),
    not here — that's what makes ctrl+o toggle it live instead of only affecting
    the next reply."""
    hint = " — press ctrl+o to view the reasoning below" if has_reasoning else ""
    console.print(f"[dim]Thought for {elapsed:.1f}s{hint}[/]")


def reasoning_toolbar_text(reasoning: list[str], expanded: bool) -> str:
    """The part of the bottom toolbar showing the last reply's reasoning trace,
    if any and if expanded. Lives in the toolbar (not printed to scrollback) so
    ctrl+o toggling it redraws instantly — prompt_toolkit re-renders the toolbar
    on every keypress, not just on a new reply."""
    if not (expanded and reasoning):
        return ""
    trace = "\n".join(t.strip() for t in reasoning if t.strip())
    if len(trace) > MAX_TOOLBAR_REASONING_CHARS:
        trace = trace[:MAX_TOOLBAR_REASONING_CHARS].rstrip() + " … (truncated)"
    return "\n" + trace


def voice_toolbar_hint(recording: bool, status: str = "") -> str:
    """The bit of the bottom toolbar showing voice input state. Pure and
    separately testable, same pattern as reasoning_toolbar_text -- covers
    both the case where the optional dependencies aren't installed (so the
    ctrl+v hint doesn't dangle a promise the key press can't keep), the
    live recording indicator, and a transient `status` ("transcribing", or
    an error such as the model not being downloaded yet)."""
    if not voice.is_available():
        return ""
    if recording:
        return "   [ctrl+v: recording — press again to stop]"
    if status == "transcribing":
        return "   [transcribing...]"
    if status:
        return f"   [{status}]"
    return "   [ctrl+v: voice input]"


def confirm_run(action: str, status: Status | None = None) -> bool:
    # Confirm.ask() blocks on real terminal input. A Status spinner's Live render
    # keeps repainting the same region in the background, so asking while one is
    # active makes the prompt invisible — R.A.V.E.N looks stuck while it's really
    # just waiting on a question you can't see. Pause the spinner around the
    # prompt, then resume it.
    if status is not None:
        status.stop()
    try:
        return Confirm.ask(f"R.A.V.E.N wants to [bold]{action}[/]. Allow?", default=False)
    finally:
        if status is not None:
            status.start()


class SlashCompleter(Completer):
    """Suggests slash commands, dimmed, when the input starts with '/'."""

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        for command in COMMANDS:
            if command.startswith(text):
                yield Completion(command, start_position=-len(text))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="raven", description="R.A.V.E.N — a terminal agent.")
    parser.add_argument(
        "-p", "--prompt",
        help="Run one request non-interactively, print the reply, then exit "
             "(no TTY needed — for scripting and testing).",
    )
    parser.add_argument(
        "-y", "--yes", action="store_true",
        help="With --prompt, auto-approve confirmation-gated tool calls "
             "(delete_file, run_command, etc.). Without this, they're denied "
             "by default — headless mode never silently approves something "
             "consequential just because no one's watching.",
    )
    return parser.parse_args(argv)


def is_exit_command(user_input: str) -> bool:
    """Whether `user_input` (already stripped) should end the session.
    "/exit"/"/quit" match on the first word regardless of trailing text
    ("/exit now" exits) -- a "/" prefix is unambiguously a command in this
    app, never conversational text, so this is always safe. Bare
    "exit"/"quit" stay a WHOLE-INPUT exact match, deliberately not extended
    the same way: a sentence can genuinely start with the word "exit"
    ("exit code 1 means what?") and must reach the model, not get silently
    swallowed as an app-exit. Pulled out as its own function (not inlined
    in main()'s loop) specifically so it's covered by a test -- main()
    itself needs a real TTY and isn't."""
    command = user_input.partition(" ")[0].lower()
    return command in ("/exit", "/quit") or user_input.lower() in ("exit", "quit")


def build_assistant(confirm_run_fn) -> tuple[Assistant, OpenRouterProvider]:
    """Construct the provider + Assistant. Shared by the interactive REPL and
    headless -p mode so the two can't drift apart. Populates the module-level
    SETTINGS/ASSISTANT globals (used by /statusline, /forget, the toolbar)."""
    global ASSISTANT
    SETTINGS.clear()
    SETTINGS.update(load_settings())
    model_cfg = SETTINGS["model"]
    provider = OpenRouterProvider(
        api_key=get_api_key(),
        model=model_cfg["name"],
        fallbacks=model_cfg.get("fallbacks", []),
    )
    assistant = ASSISTANT = Assistant(provider, confirm_run=confirm_run_fn, store=Store(), settings=SETTINGS)
    return assistant, provider


def run_headless(prompt: str, auto_confirm: bool = False) -> None:
    """Run one request and print the plain-text reply, then exit — no TTY,
    no PromptSession, no Rich styling, so the output stays pipeable/scriptable.
    Confirmation-gated tool calls are DENIED by default (never silently
    approved just because no one's watching); pass auto_confirm to opt in."""
    confirm = (lambda action: True) if auto_confirm else (lambda action: False)
    assistant, _ = build_assistant(confirm)
    try:
        reply = assistant.ask(prompt)
    except Exception as e:
        print(f"I ran into an error: {e}")
        raise SystemExit(1)
    print(reply)
    
def schedule_word_rotation(status, words, interval=5.0, shimmer_interval=0.12):
    """Drives the "thinking" status while waiting on the model: a random
    word from `words` every `interval` seconds, with a shimmer sweep
    animated across it every `shimmer_interval` seconds in between.

    Returns (stop, pause). stop() cancels everything. pause() freezes the
    rotation/shimmer in place WITHOUT cancelling the timer chain — this is
    the fix for the tool-status-getting-overwritten bug: Assistant.on_tool_call
    replaces the status text with what a running tool is doing, but the timers
    here kept firing on their own schedule and stomping it with a random word
    up to `interval` seconds later. main() calls pause() from on_tool_call, so
    once a tool call happens, the rotation stops touching the status for the
    rest of this turn and the tool's own text (set directly via status.update,
    not through this function) sticks until the turn ends or another tool
    call replaces it."""
    stopped = {"flag": False}
    paused = {"flag": False}
    state = {"word_idx": -1, "frame": 0}
    timers = []

    def pick_word():
        idx = randint(0, len(words) - 1)
        while idx == state["word_idx"] and len(words) > 1:
            idx = randint(0, len(words) - 1)
        state["word_idx"] = idx
        state["frame"] = 0

    def render():
        word = words[state["word_idx"]]
        text = shimmer_text(word, state["frame"]) if word else ""
        try:
            status.update(f"{text} [dim](Ctrl+C to cancel)[/]" if text else "[dim](Ctrl+C to cancel)[/]")
        except Exception:
            pass

    def word_tick():
        if stopped["flag"]:
            return
        if not paused["flag"]:
            pick_word()
            render()
        t = threading.Timer(interval, word_tick)
        t.daemon = True
        t.start()
        timers.append(t)

    def shimmer_tick():
        if stopped["flag"]:
            return
        if not paused["flag"]:
            state["frame"] += 1
            render()
        t = threading.Timer(shimmer_interval, shimmer_tick)
        t.daemon = True
        t.start()
        timers.append(t)

    pick_word()
    t1 = threading.Timer(interval, word_tick)
    t1.daemon = True
    t1.start()
    timers.append(t1)
    t2 = threading.Timer(shimmer_interval, shimmer_tick)
    t2.daemon = True
    t2.start()
    timers.append(t2)

    def stop():
        stopped["flag"] = True
        for timer in timers:
            timer.cancel()

    def pause():
        paused["flag"] = True

    return stop, pause


def main():
    args = parse_args()
    if args.prompt is not None:
        run_headless(args.prompt, auto_confirm=args.yes)
        return

    assistant, provider = build_assistant(confirm_run)

    def bottom_toolbar():
        thoughts = "expanded" if UI_STATE["show_thoughts"] else "collapsed"
        header = (statusline.render(provider, assistant, SETTINGS["statusline"]["segments"])
                  + f"   [ctrl+o: thoughts {thoughts}]" + voice_toolbar_hint(UI_STATE["recording"], UI_STATE["voice_status"]))
        return header + reasoning_toolbar_text(assistant.last_reasoning, UI_STATE["show_thoughts"])

    session = PromptSession(
        completer=SlashCompleter(),
        style=PROMPT_STYLE,
        complete_while_typing=True,
        key_bindings=KEY_BINDINGS,
        bottom_toolbar=bottom_toolbar,
        refresh_interval=2,
    )

    console.print("""
█▀█   ▄▀█   █ █   █▀▀   █▄ █
█▀▄ ▄ █▀█ ▄ ▀▄▀ ▄ ██▄ ▄ █ ▀█
""", style=primary_color)

    console.print(f"[bold {primary_color}]R.A.V.E.N[/] — {STARTUP_SENTENCES[randint(0, len(STARTUP_SENTENCES)-1)]} [dim](type '/help' for commands, '/exit' to quit)[/]")
    if assistant.history:
        console.print(f"[dim]Resumed {len(assistant.history)} messages from earlier sessions.[/]")
    _, rules_paths = load_project_rules()
    if rules_paths:
        console.print(f"[dim]Loaded rules from {', '.join(str(p) for p in rules_paths)}.[/]")
    console.print()

    while True:
        try:
            user_input = session.prompt("› ").strip()
        except KeyboardInterrupt:
            # Ctrl+C at the prompt: discard the line, stay in the session
            # (like a shell) instead of dying with a traceback.
            console.print("[dim](Ctrl+D or /exit to quit)[/]")
            continue
        except EOFError:  # Ctrl+D
            console.print("\n[dim]R.A.V.E.N: Standing by.[/]")
            break
        if not user_input:
            continue
        if is_exit_command(user_input):
            console.print("\n[dim]R.A.V.E.N: Standing by.[/]")
            break

        command, _, args = user_input.partition(" ")
        if command in COMMANDS:
            COMMANDS[command](args)
            continue

        initial_word = THINKING_WORDS[randint(0, len(THINKING_WORDS) - 1)]

        with Status(f"[dim]{initial_word} (Ctrl+C to cancel)[/]", console=console, spinner="dots8Bit") as status:
            stop_rotation, pause_rotation = schedule_word_rotation(status, THINKING_WORDS, interval=5.0)

            def on_tool_call(name, args):
                # Pause first: a tool is now running, so the rotation must
                # stop overwriting this with a random "thinking" word — the
                # bug this whole change fixes (previously the rotation timer
                # could stomp this update up to 5s later, mid-tool-run).
                pause_rotation()
                status.update(f"[dim]{tool_status_text(name, args)} (Ctrl+C to cancel)[/]")

            assistant.on_tool_call = on_tool_call
            assistant.confirm_run = lambda action: confirm_run(action, status)
            thought_ok = True
            try:
                reply = assistant.ask(user_input)
            except KeyboardInterrupt:
                console.print("\n[dim]Cancelled.[/dim]\n")
                stop_rotation()
                continue
            except Exception as e:
                reply = f"I ran into an error: {e}"
                thought_ok = False
            finally:
                stop_rotation()

        if thought_ok:
            render_thought_line(assistant.last_elapsed, bool(assistant.last_reasoning))

        console.print(f"[bold {primary_color}]R.A.V.E.N[/]")
        console.print(Markdown(reply))
        console.print()


if __name__ == "__main__":
    main()