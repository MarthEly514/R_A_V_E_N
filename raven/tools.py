"""Execution primitives: file I/O and shell commands."""
import difflib
import html
import json
import re
import subprocess
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from raven import browser as _browser
from raven import gmail as _gmail
from raven import platforms
from raven.config import NOTES_PATH

_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; RAVEN-agent/1.0)"}

# Same 4000-char convention already used by fetch_url/browser.read/gmail's body
# (see MAX_READ_CHARS, MAX_BODY_CHARS) — applied here to the tools whose output
# is otherwise genuinely unbounded (a big file, a build log, a huge directory).
MAX_OUTPUT_CHARS = 4000


def _truncate(text: str, max_chars: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f" … (truncated, {len(text) - max_chars} more chars)"


def resolve(path: str) -> Path:
    return Path(path).expanduser().resolve()


def _is_existing_file(path: str) -> bool:
    """Whether `path` resolves to an existing file — used to decide whether
    write_file needs confirmation (creating a new file doesn't; overwriting
    one does). Fails safe (True, i.e. "ask") on any error resolving the
    path, rather than silently skipping confirmation."""
    try:
        return bool(path) and resolve(path).is_file()
    except (OSError, ValueError):
        return True


def read_file(path: str) -> str:
    return _truncate(resolve(path).read_text())


def write_file(path: str, content: str = "") -> str:
    resolve(path).write_text(content)
    return f"Wrote {path}"


def make_dir(path: str) -> str:
    resolve(path).mkdir(parents=True, exist_ok=True)
    return f"Created {path}"


# save_note (B3): durable, cross-session memory — separate from conversation
# history (which /forget wipes and A2/B2's budget trims/compacts) and from
# RAVEN.md (user-authored instructions). Notes are model-written FACTS the
# model chose to remember beyond this conversation, loaded into every future
# session's system prompt (see llm_provider.load_notes).
MAX_NOTE_CHARS = 500  # a single note is meant to be one concise fact, not an essay
MAX_NOTES_FILE_CHARS = 4000  # same convention as RULES_MAX_CHARS — fixed per-request overhead


def save_note(note: str) -> str:
    """Append `note` as one line to ~/.raven/memory/notes.md, creating the
    file/directory if needed. Refuses (doesn't silently truncate or evict
    older notes) if the note itself is too long or the file is already at
    its cap — a fact silently cut off or a still-relevant note silently
    dropped would be worse than an explicit error the model can act on."""
    note = note.strip()
    if not note:
        return "Error: empty note."
    note = note.replace("\r", " ").replace("\n", " ")  # one note = one line, keeps the file greppable
    if len(note) > MAX_NOTE_CHARS:
        return f"Error: note too long ({len(note)} chars, max {MAX_NOTE_CHARS}). Keep it to one concise fact."
    NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = NOTES_PATH.read_text() if NOTES_PATH.exists() else ""
    line = f"- {note}\n"
    if len(existing) + len(line) > MAX_NOTES_FILE_CHARS:
        return (f"Error: memory is full ({MAX_NOTES_FILE_CHARS}-char cap). "
                f"Ask the user before removing old notes to make room.")
    NOTES_PATH.write_text(existing + line)
    return f"Saved note: {note}"


def analyze_image(path: str, question: str = "Describe this image.") -> str:
    """Placeholder only (V2) -- Assistant._run_tool intercepts this tool
    name BEFORE it ever reaches TOOL_FUNCTIONS, the same way load_skill is
    intercepted, since it needs a live provider to make a real vision-model
    call, which a plain tool function has no access to. Registered here
    anyway (spec + this placeholder) so it flows through the normal skill
    gating (tools.SKILLS/core_tool_names) and registry-completeness checks
    like every other tool, rather than needing its own special case there
    too -- the special case is confined to _run_tool's dispatch, nowhere
    else. Should never actually execute; if it does, the interception in
    assistant.py was skipped somehow, and this says so plainly instead of
    silently doing the wrong thing."""
    raise RuntimeError(
        "analyze_image must be handled by Assistant (it needs a live provider for the "
        "real vision-model call) -- this placeholder should never run directly."
    )


def list_dir(path: str = ".") -> str:
    entries = sorted(resolve(path).iterdir())
    return _truncate("\n".join(f"{'d' if e.is_dir() else '-'} {e.name}" for e in entries) or "(empty)")


def edit_file(path: str, old_string: str, new_string: str) -> str:
    """Replace an EXACT, UNIQUE occurrence of old_string with new_string in
    the file at path (C1) -- targeted edits without rewriting a whole file
    through write_file. Refuses (no write happens) if old_string isn't found
    or is found more than once: an ambiguous target could silently edit the
    wrong spot, so the caller has to supply enough surrounding context to
    pin down exactly one location, same semantics as Claude Code's own edit
    tool. Always requires confirmation (see assistant._confirm_prompt) --
    like write_file's overwrite case, this only ever touches a file that
    already exists."""
    if not old_string:
        return "Error: old_string must not be empty."
    file = resolve(path)
    content = file.read_text()
    count = content.count(old_string)
    if count == 0:
        return f"Error: old_string not found in {path}."
    if count > 1:
        return f"Error: old_string matches {count} times in {path} -- include more context to make it unique."
    file.write_text(content.replace(old_string, new_string, 1))
    return f"Edited {path}"


# glob_files / tree (C1): read-only navigation/search tools, same skip-dirs
# and result-cap conventions as grep so a huge or deep tree can't produce
# unbounded output.
MAX_GLOB_RESULTS = 200
MAX_TREE_ENTRIES = 500


def glob_files(pattern: str, path: str = ".") -> str:
    """Find files under `path` matching a glob pattern (e.g. "**/*.py"),
    one relative path per line. Read-only, no shell involved -- same
    rationale as grep for never needing confirmation."""
    base = resolve(path)
    if not base.is_dir():
        return f"Not a directory: {path}"
    results = []
    for file in sorted(base.glob(pattern)):
        if not file.is_file() or any(part in _GREP_SKIP_DIRS for part in file.relative_to(base).parts):
            continue
        results.append(str(file.relative_to(base)))
        if len(results) >= MAX_GLOB_RESULTS:
            break
    if not results:
        return f"No files matching '{pattern}' in {path}."
    result = "\n".join(results)
    if len(results) >= MAX_GLOB_RESULTS:
        result += f"\n… (stopped at {MAX_GLOB_RESULTS} results — narrow the pattern)"
    return _truncate(result)


def tree(path: str = ".", max_depth: int = 3) -> str:
    """Recursive directory listing (d = dir, - = file), indented by depth,
    skipping the same heavy directories grep does, bounded to max_depth
    levels and MAX_TREE_ENTRIES total entries."""
    base = resolve(path)
    if not base.is_dir():
        return f"Not a directory: {path}"
    lines: list[str] = []

    def walk(dir_path: Path, depth: int) -> None:
        if len(lines) >= MAX_TREE_ENTRIES:
            return
        try:
            entries = sorted(dir_path.iterdir(), key=lambda p: (p.is_file(), p.name))
        except OSError:
            return
        for entry in entries:
            if len(lines) >= MAX_TREE_ENTRIES:
                return
            if entry.name in _GREP_SKIP_DIRS:
                continue
            lines.append(f"{'  ' * depth}{'d' if entry.is_dir() else '-'} {entry.name}")
            if entry.is_dir() and depth < max_depth:
                walk(entry, depth + 1)

    walk(base, 0)
    result = "\n".join(lines) or "(empty)"
    if len(lines) >= MAX_TREE_ENTRIES:
        result += f"\n… (stopped at {MAX_TREE_ENTRIES} entries — narrow the path or depth)"
    return _truncate(result)


# grep: directories skipped outright (heavy, rarely what a code search wants),
# and a total-files-scanned cap as a second safety net beyond the match cap,
# so a rare pattern over a huge unfiltered tree can't turn into a slow scan.
_GREP_SKIP_DIRS = {".git", "venv", "node_modules", "__pycache__", ".pytest_cache",
                    "raven.egg-info", "dist", "build", ".mypy_cache", ".ruff_cache"}
MAX_GREP_MATCHES = 50
MAX_GREP_FILES_SCANNED = 5000


def _looks_binary(path: Path) -> bool:
    """Same heuristic grep/git use: a NUL byte in the first KB means binary."""
    try:
        with path.open("rb") as f:
            return b"\0" in f.read(1024)
    except OSError:
        return True  # unreadable -> skip rather than guess


def grep(pattern: str, path: str = ".", glob: str = "**/*") -> str:
    """Search text files under `path` for lines matching a regex `pattern`
    (case-insensitive), narrowed by `glob` (e.g. "**/*.py"). Read-only —
    doesn't shell out, so unlike routing this through run_command there's no
    injection surface to guard, hence no confirmation needed."""
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return f"Invalid pattern: {e}"
    base = resolve(path)
    if not base.is_dir():
        return f"Not a directory: {path}"
    matches = []
    scanned = 0
    for file in sorted(base.glob(glob)):
        if not file.is_file() or any(part in _GREP_SKIP_DIRS for part in file.relative_to(base).parts):
            continue
        scanned += 1
        if scanned > MAX_GREP_FILES_SCANNED:
            break
        if _looks_binary(file):
            continue
        try:
            text = file.read_text(errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                matches.append(f"{file.relative_to(base)}:{lineno}: {line.strip()}")
                if len(matches) >= MAX_GREP_MATCHES:
                    break
        if len(matches) >= MAX_GREP_MATCHES:
            break
    if not matches:
        return f"No matches for '{pattern}' in {path}."
    result = "\n".join(matches)
    if len(matches) >= MAX_GREP_MATCHES:
        result += f"\n… (stopped at {MAX_GREP_MATCHES} matches — narrow the pattern or glob)"
    return _truncate(result)


def delete_file(path: str) -> str:
    resolve(path).unlink()
    return f"Deleted {path}"


RUN_COMMAND_TIMEOUT = 30
# C3: a real test suite (this project's own included, once its live/playwright
# tests are counted) routinely runs longer than a quick shell command --
# run_tests gets its own, generously longer timeout rather than forcing every
# run_command call to wait that long just to accommodate the rare slow case.
RUN_TESTS_TIMEOUT = 120


def _run_shell(command: str, timeout: int) -> str:
    # stdin=DEVNULL is load-bearing, not decoration: without it, subprocess.run
    # inherits R.A.V.E.N's own stdin -- the user's REAL terminal in interactive
    # mode. Any command that tries to read input (python3 with no args, ssh,
    # sudo, git commit with no -m, less/vim/nano, a bare `read` in a script,
    # ...) then blocks reading from that terminal, and since prompt_toolkit
    # already owns the terminal in its own raw input mode for its own
    # line-editing, a stray Ctrl+C doesn't reach the blocked subprocess as a
    # normal SIGINT the way it would in a plain shell -- the whole session can
    # appear to hang completely, unrecoverably, well past whatever `timeout`
    # is set to (confirmed live: reproduced the exact hang via a real pty,
    # confirmed DEVNULL fixes it in ~5ms instead of blocking for the full
    # timeout). No legitimate run_command/run_tests/git use case needs
    # interactive stdin -- the model can't type into it, so there was never
    # a good reason to leave this open.
    result = subprocess.run(
        command, shell=True, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
    )
    output = result.stdout + result.stderr
    return _truncate(output.strip()) or f"(exited {result.returncode}, no output)"


def run_command(command: str) -> str:
    return _run_shell(command, RUN_COMMAND_TIMEOUT)


def run_tests(command: str = "pytest") -> str:
    """Like run_command, but with a longer timeout for a real test suite and
    a distinct tool identity so the model reaches for this instead of being
    tempted to squeeze a slow test run through run_command's 30s cap.
    Confirmation policy is identical to run_command's (see
    assistant._confirm_prompt) -- settings.json's permissions.allow_commands
    (C3) is what lets a project's test command skip confirmation, e.g.
    {"allow_commands": ["pytest"]}."""
    return _run_shell(command, RUN_TESTS_TIMEOUT)


def git(args: str = "status") -> str:
    return run_command(f"git {args}")


def _normalize_url(url: str) -> str:
    return url if url.startswith(("http://", "https://")) else f"https://{url}"


def open_url(url: str, app: str = "") -> str:
    """Open a URL, either with the system default browser or a specific named
    browser/app (e.g. to open it in Firefox when that isn't the default)."""
    url = _normalize_url(url)
    if not app:
        return f"Opened {url} in the browser." if webbrowser.open(url) else f"Could not open {url}."
    return _launch_app_with_target(app, url, url)


def _strip_html(raw: str, max_chars: int = 4000) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def fetch_url(url: str) -> str:
    """Fetch a URL and return its text content, HTML stripped and truncated."""
    url = _normalize_url(url)
    try:
        response = requests.get(url, headers=_HTTP_HEADERS, timeout=10)
    except requests.RequestException as e:
        return f"Could not fetch {url}: {e}"
    if not response.ok:
        return f"Fetching {url} failed: HTTP {response.status_code}"
    return _strip_html(response.text) or "(empty page)"


def _unwrap_ddg_link(href: str) -> str:
    """DuckDuckGo's HTML results wrap links in a redirect; pull the real URL out."""
    if href.startswith("//"):
        href = "https:" + href
    query = parse_qs(urlparse(href).query)
    return query.get("uddg", [href])[0]


def web_search(query: str) -> str:
    """Search the web (DuckDuckGo) and return the top results' titles, URLs, and snippets."""
    try:
        response = requests.get(
            "https://html.duckduckgo.com/html/", params={"q": query},
            headers=_HTTP_HEADERS, timeout=10,
        )
    except requests.RequestException as e:
        return f"Search failed: {e}"
    if response.status_code == 202:
        return ("Web search is temporarily rate-limited by the search provider. Try again "
                "shortly, or use fetch_url on a specific page instead.")
    if not response.ok:
        return f"Search failed: HTTP {response.status_code}"
    results = re.findall(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        response.text, flags=re.S,
    )
    if not results:
        return f"No results for '{query}'."
    lines = []
    for href, title, snippet in results[:5]:
        clean_title = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
        clean_snippet = html.unescape(re.sub(r"<[^>]+>", "", snippet)).strip()
        lines.append(f"- {clean_title}\n  {_unwrap_ddg_link(href)}\n  {clean_snippet}")
    return "\n".join(lines)


def list_apps() -> str:
    """List every installed application's display name, so the caller can pick
    the exact one to pass to open_app/open_file instead of guessing."""
    apps = platforms.current().installed_apps()
    return "\n".join(sorted(n.title() for n in apps)) or "(no apps found)"


def _contains_words(haystack: str, needle: str) -> bool:
    """True if every word of `needle` appears as a whole word in `haystack`
    (word-boundary, not raw substring — a one-letter app name like "R" would
    otherwise match almost any query just by appearing inside another word)."""
    haystack_words = set(re.findall(r"[a-z0-9]+", haystack))
    needle_words = re.findall(r"[a-z0-9]+", needle)
    return bool(needle_words) and all(w in haystack_words for w in needle_words)


def _match_app(name: str) -> tuple[str, str] | str:
    """Resolve a (fuzzy) app name to (display name, app id), or an error string."""
    apps = platforms.current().installed_apps()
    query = name.strip().lower()
    # Word-containment works both ways, so extra words ("the text editor app") or a
    # shorter query ("editor") both still find "text editor".
    matches = [n for n in apps if query == n] or [
        n for n in apps if _contains_words(n, query) or _contains_words(query, n)
    ]
    if not matches:
        close = difflib.get_close_matches(query, apps.keys(), n=5, cutoff=0.4)
        if close:
            return (f"No installed app matches '{name}'. Closest installed names: "
                     f"{', '.join(n.title() for n in close)}. Use one of these, or call "
                     f"list_apps to see everything installed.")
        return f"No installed app matches '{name}'. Call list_apps to see what's installed."
    if len(matches) > 1:
        candidates = ", ".join(sorted(n.title() for n in matches)[:10])
        return f"Multiple apps match '{name}': {candidates}. Ask which one, or be more specific."
    matched_name = matches[0]
    return matched_name, apps[matched_name]


def open_app(name: str) -> str:
    """Launch an installed desktop application by (fuzzy) name."""
    match = _match_app(name)
    if isinstance(match, str):
        return match
    matched_name, app_id = match
    error = platforms.current().launch(app_id)
    return error or f"Launched {matched_name.title()}."


def _launch_app_with_target(app: str, target: str, label: str) -> str:
    """Resolve `app` (fuzzy name) and launch it with `target` (a file path or
    URL) as its argument. `label` is what the success message shows. Shared by
    open_file and open_url's app= path."""
    match = _match_app(app)
    if isinstance(match, str):
        return match
    matched_name, app_id = match
    error = platforms.current().launch(app_id, target)
    return error or f"Opened {label} in {matched_name.title()}."


def open_file(path: str, app: str = "") -> str:
    """Open a file, either with the system default app or a named one."""
    target = resolve(path)
    if not target.exists():
        return f"No such file: {path}"
    if not app:
        error = platforms.current().open_default(str(target))
        return error or f"Opened {path} with the default app."
    return _launch_app_with_target(app, str(target), path)


def close_app(name: str) -> str:
    """Close a running application by (fuzzy) name. Requires user confirmation.

    Closes EVERY window of the app at once. To close just one window, use
    close_window instead (needs OS window-control support -- on Linux, the
    "Window Calls" GNOME Shell extension)."""
    match = _match_app(name)
    if isinstance(match, str):
        return match
    matched_name, app_id = match
    return platforms.current().close_app(app_id, matched_name)


def list_windows() -> str:
    """List every open window: id, owning app, and title. Use this to find a
    window's id/title before calling close_window on it. If window control isn't
    available (unsupported OS, missing extension/permission), says so plainly
    instead of failing silently."""
    try:
        windows = platforms.current().list_windows()
    except platforms.WindowControlError as e:
        return f"Can't list windows: {e}."
    if not windows:
        return "(no windows found)"
    return "\n".join(f"{w['id']} | {w['app']} | {w['title']}" for w in windows)


def close_window(title: str) -> str:
    """Close exactly one window, matched by a case-insensitive substring of its
    title (see list_windows for the exact titles). Requires user confirmation.
    Unlike close_app (which always closes an app's every window), this closes
    only the one matching window. If window control isn't available, says so."""
    backend = platforms.current()
    try:
        windows = backend.list_windows()
    except platforms.WindowControlError as e:
        return f"Can't close window: {e}."
    query = title.strip().lower()
    matches = [w for w in windows if query in (w.get("title") or "").lower()]
    if not matches:
        return f"No open window matches '{title}'. Call list_windows to see what's open."
    if len(matches) > 1:
        candidates = "; ".join(f"{w['id']}: {w['title']}" for w in matches[:10])
        return f"Multiple windows match '{title}': {candidates}. Be more specific."
    win = matches[0]
    try:
        backend.close_window(win["id"])
    except platforms.WindowControlError as e:
        return f"Couldn't close window: {e}."
    return f"Closed window: {win['title']}"


TOOL_FUNCTIONS = {
    "read_file": read_file,
    "write_file": write_file,
    "make_dir": make_dir,
    "save_note": save_note,
    "list_dir": list_dir,
    "edit_file": edit_file,
    "glob_files": glob_files,
    "tree": tree,
    "grep": grep,
    "delete_file": delete_file,
    "run_command": run_command,
    "run_tests": run_tests,
    "git": git,
    "open_app": open_app,
    "open_file": open_file,
    "close_app": close_app,
    "list_windows": list_windows,
    "close_window": close_window,
    "list_apps": list_apps,
    "open_url": open_url,
    "fetch_url": fetch_url,
    "web_search": web_search,
    "browser_navigate": _browser.navigate,
    "browser_read": _browser.read,
    "browser_click": _browser.click,
    "browser_type": _browser.type_text,
    "browser_submit": _browser.submit,
    "browser_close": _browser.close,
    "browser_screenshot": _browser.screenshot,
    "analyze_image": analyze_image,
    "gmail_list_messages": _gmail.list_messages,
    "gmail_read_message": _gmail.read_message,
    "gmail_send_message": _gmail.send_message,
    "gmail_reply_message": _gmail.reply_message,
}

TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read and return the contents of a text file, truncated to ~4000 characters "
                       "for a large file (a truncation marker says how much was cut).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Path to the file"}},
            "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Write a text file with the given content. Requires user confirmation if "
                       "the file already exists (overwriting it) — not if it's new.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Path to the file"},
            "content": {"type": "string", "description": "Content to write"}},
            "required": ["path", "content"]},
    }},
    {"type": "function", "function": {
        "name": "make_dir",
        "description": "Create a directory, including parent directories if needed.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory path to create"}},
            "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "save_note",
        "description": "Save one concise fact to durable, cross-session memory (max ~500 "
                       "characters) — use this for something worth remembering beyond this "
                       "conversation (a user preference, a project convention, a correction), "
                       "not for routine conversation content. Loaded automatically at the start "
                       "of every future session. Never needs confirmation. Refuses if the note "
                       "is too long or memory is already full, rather than silently truncating.",
        "parameters": {"type": "object", "properties": {
            "note": {"type": "string", "description": "The fact to remember, as one concise sentence"}},
            "required": ["note"]},
    }},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List the entries of a directory (d = dir, - = file), truncated to ~4000 "
                       "characters for a very large directory.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory path (default: current dir)"}}},
    }},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Replace an exact, UNIQUE occurrence of old_string with new_string in an "
                       "existing file. Prefer this over write_file for a targeted change to part "
                       "of a file — it fails safely if old_string isn't found, or matches more "
                       "than once (add more surrounding context to make it unique). Always "
                       "requires user confirmation, since it only ever edits an existing file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Path to the file"},
            "old_string": {"type": "string", "description": "Exact text to replace (must match exactly once)"},
            "new_string": {"type": "string", "description": "Text to replace it with"}},
            "required": ["path", "old_string", "new_string"]},
    }},
    {"type": "function", "function": {
        "name": "glob_files",
        "description": "Find files under a directory matching a glob pattern (e.g. \"**/*.py\"), "
                       "one relative path per line — use this to locate files by name/extension "
                       "rather than content (for content, use grep). Read-only, never needs "
                       "confirmation. Common directories (.git, venv, node_modules, etc.) are "
                       "skipped automatically; results capped at 200 — narrow the pattern if you hit that.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py'"},
            "path": {"type": "string", "description": "Directory to search under (default: current dir)"}},
            "required": ["pattern"]},
    }},
    {"type": "function", "function": {
        "name": "tree",
        "description": "Show a directory's structure recursively (d = dir, - = file), indented by "
                       "depth — use this to get oriented in an unfamiliar project instead of "
                       "repeated list_dir calls. Bounded to 3 levels deep by default and 500 total "
                       "entries; common heavy directories are skipped automatically.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory path (default: current dir)"},
            "max_depth": {"type": "integer", "description": "How many levels deep to recurse (default: 3)"}}},
    }},
    {"type": "function", "function": {
        "name": "grep",
        "description": "Search text files for lines matching a regex pattern (case-insensitive), "
                       "e.g. to find where something is defined or used in a codebase. Prefer "
                       "this over run_command with grep/find — this one never needs confirmation, "
                       "since it's read-only and doesn't run a shell command. Common directories "
                       "(.git, venv, node_modules, __pycache__, etc.) are skipped automatically; "
                       "results capped at 50 matches — narrow the pattern or glob if you hit that.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Regex to search for"},
            "path": {"type": "string", "description": "Directory to search under (default: current dir)"},
            "glob": {"type": "string", "description": "Glob to narrow which files are searched, "
                                                        "e.g. '**/*.py' (default: all files)"}},
            "required": ["pattern"]},
    }},
    {"type": "function", "function": {
        "name": "delete_file",
        "description": "Delete a single file. Requires user confirmation.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Path to the file"}},
            "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "run_command",
        "description": "Run a shell command and return its output, truncated to ~4000 characters "
                       "for a very long output. Requires user confirmation, except for a plain "
                       "(no chaining/redirection) call to a read-only command like ls, cat, ps, "
                       "pwd, wc, head, tail, which, id, date, uname, df, du, hostname, free, "
                       "uptime, whoami, or echo, which runs immediately.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "Shell command to run"}},
            "required": ["command"]},
    }},
    {"type": "function", "function": {
        "name": "run_tests",
        "description": "Run a test command (default: 'pytest') with a longer timeout (120s) than "
                       "run_command's 30s, for a real test suite. Prefer this over run_command "
                       "whenever the goal is specifically running tests. Same confirmation policy "
                       "as run_command — asks unless the command is on the user's trusted list.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "Test command to run (default: 'pytest')"}}},
    }},
    {"type": "function", "function": {
        "name": "git",
        "description": "Run a git subcommand in the current repo, e.g. \"status\", \"log --oneline -5\", "
                       "\"add -A\", \"commit -m msg\". Write subcommands require user confirmation.",
        "parameters": {"type": "object", "properties": {
            "args": {"type": "string", "description": "Arguments after 'git' (default: status)"}}},
    }},
    {"type": "function", "function": {
        "name": "open_app",
        "description": "Open an installed desktop application by name (matched against installed "
                       "app names, e.g. \"firefox\", \"files\", \"code\"). No confirmation needed. "
                       "If the name is ambiguous or unknown, returns candidate names instead of "
                       "guessing — use list_apps if you're not sure of the exact name.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Application name or part of it"}},
            "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "open_file",
        "description": "Open an existing file, optionally in a specific installed app (matched "
                       "the same way as open_app). Without 'app', opens with the system default "
                       "handler for that file type. No confirmation needed. To create a new file "
                       "and open it, call write_file first, then this.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Path to the file"},
            "app": {"type": "string", "description": "App to open it with (optional)"}},
            "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "close_app",
        "description": "Close a running application by name (matched the same way as open_app). "
                       "Requires user confirmation, since it can lose unsaved work in that app. "
                       "Always closes EVERY window of the app at once. If the user wants to close "
                       "only one window of a multi-window app (e.g. one of several VS Code or "
                       "browser windows), use close_window instead, not this.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Application name or part of it"}},
            "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "list_windows",
        "description": "List every open window (id, owning app, title). Use this to see exact "
                       "window titles before calling close_window, or when the user refers to "
                       "'the window with X open' and you need to find which one that is.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "close_window",
        "description": "Close exactly ONE window, matched by a substring of its title (see "
                       "list_windows for exact titles). Requires user confirmation. Use this "
                       "(not close_app) whenever the user wants to close one specific window "
                       "while leaving other windows of the same app open.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "Substring of the window's title"}},
            "required": ["title"]},
    }},
    {"type": "function", "function": {
        "name": "list_apps",
        "description": "List the display names of every installed application. Call this before "
                       "open_app/open_file when unsure of the exact installed name, then pass the "
                       "matching entry — do not guess a specific brand name from general knowledge.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "open_url",
        "description": "Open a URL, either with the default web browser or a specific named "
                       "app/browser (matched the same way as open_app) — use 'app' whenever the "
                       "user names a specific browser, since the default browser may not be the "
                       "one they mean. No confirmation needed.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "URL to open (scheme optional)"},
            "app": {"type": "string", "description": "Browser/app to open it with (optional)"}},
            "required": ["url"]},
    }},
    {"type": "function", "function": {
        "name": "fetch_url",
        "description": "Fetch a web page and return its text content (HTML stripped, truncated "
                       "to ~4000 characters). Use this to read a specific page, e.g. one found "
                       "via web_search.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "URL to fetch (scheme optional)"}},
            "required": ["url"]},
    }},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web and return the top results' titles, URLs, and snippets. "
                       "Use this to answer questions about current events or anything not in your "
                       "training data or the local filesystem; follow up with fetch_url to read a "
                       "specific result in full.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Search query"}},
            "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "browser_navigate",
        "description": "Go to a URL in R.A.V.E.N's own isolated browser (no saved logins/cookies "
                       "from the user's real browser — it can't act on sites they're already "
                       "logged into). Persists across calls: later browser_* calls act on this "
                       "same page until you navigate elsewhere. Use this over fetch_url when the "
                       "page needs JavaScript to render, or you need to click/type on it "
                       "afterward. No confirmation needed.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "URL to load (scheme optional)"}},
            "required": ["url"]},
    }},
    {"type": "function", "function": {
        "name": "browser_read",
        "description": "Return the current browser page's visible text, as rendered (post-"
                       "JavaScript). Call browser_navigate first. No confirmation needed.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "browser_click",
        "description": "Click a visible button or link on the current page, matched by its text "
                       "(exact wording not required). No confirmation needed for an ordinary "
                       "click — BUT if the target actually submits a form (a real submit button), "
                       "this refuses and tells you to use browser_submit instead, since that "
                       "needs confirmation. Never try to work around that refusal by clicking a "
                       "different nearby element to trigger the same submission.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "Visible text of the button/link to click"}},
            "required": ["text"]},
    }},
    {"type": "function", "function": {
        "name": "browser_type",
        "description": "Type text into an input field on the current page, matched by its label, "
                       "placeholder, or accessible name. No confirmation needed — typing alone "
                       "doesn't submit anything.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "Label/placeholder identifying the field"},
            "value": {"type": "string", "description": "Text to type into it"}},
            "required": ["text", "value"]},
    }},
    {"type": "function", "function": {
        "name": "browser_submit",
        "description": "Click a control that COMMITS an action — submits a form, completes a "
                       "purchase, sends a message, logs in, deletes something, or similarly "
                       "finalizes something — matched by its visible text. Requires user "
                       "confirmation. Use this (not browser_click) for anything that commits, "
                       "even if browser_click didn't explicitly refuse it (e.g. a JS-driven "
                       "'Buy Now' button that isn't a literal <button type=submit>).",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "Visible text of the control to activate"}},
            "required": ["text"]},
    }},
    {"type": "function", "function": {
        "name": "browser_close",
        "description": "Close R.A.V.E.N's browser session (frees memory). No confirmation needed "
                       "— it only closes R.A.V.E.N's own isolated browser, nothing of the user's.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "browser_screenshot",
        "description": "Save a screenshot (PNG) of the current browser page to a temp file and "
                       "return its path. Capture only — follow up with analyze_image(path, "
                       "question) to actually see/interpret what's in it. No confirmation needed.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "analyze_image",
        "description": "Look at an image file (a screenshot, photo, or diagram) and answer a "
                       "question about it, using a separate vision-capable model. Use this "
                       "whenever a request needs you to actually SEE something, not just read "
                       "text about it. No confirmation needed (read-only).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Path to the image file"},
            "question": {"type": "string",
                        "description": "What to answer about the image (default: describe it)"}},
            "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "gmail_list_messages",
        "description": "List the user's recent Gmail messages (id, date, sender, subject), "
                       "optionally filtered by a Gmail search query (e.g. 'is:unread', "
                       "'from:someone@example.com', 'newer_than:7d'). Read-only — cannot send, "
                       "delete, or modify anything. No confirmation needed. If Gmail isn't set up "
                       "yet, returns a clear message saying so rather than an error.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Gmail search query (optional, default: inbox)"},
            "max_results": {"type": "integer", "description": "Max messages to return (default 10)"}}},
    }},
    {"type": "function", "function": {
        "name": "gmail_read_message",
        "description": "Read one Gmail message's sender, date, subject, and body text, by id "
                       "(from gmail_list_messages). Read-only. No confirmation needed.",
        "parameters": {"type": "object", "properties": {
            "message_id": {"type": "string", "description": "Message id from gmail_list_messages"}},
            "required": ["message_id"]},
    }},
    {"type": "function", "function": {
        "name": "gmail_send_message",
        "description": "Send a NEW email as the user (starts a new thread). To answer an email "
                       "that already exists, use gmail_reply_message instead, not this. Requires "
                       "user confirmation — never claim it was sent unless the tool result "
                       "actually confirms that.",
        "parameters": {"type": "object", "properties": {
            "to": {"type": "string", "description": "Recipient email address"},
            "subject": {"type": "string", "description": "Email subject"},
            "body": {"type": "string", "description": "Email body text"}},
            "required": ["to", "subject", "body"]},
    }},
    {"type": "function", "function": {
        "name": "gmail_reply_message",
        "description": "Reply to an existing email, in its own thread, to whoever it says to "
                       "reply to (the recipient is worked out from the original, not chosen by "
                       "you). Plain reply only — not reply-all. Get the message_id from "
                       "gmail_list_messages. Requires user confirmation — never claim it was "
                       "sent unless the tool result actually confirms that.",
        "parameters": {"type": "object", "properties": {
            "message_id": {"type": "string", "description": "Id of the message being replied to"},
            "body": {"type": "string", "description": "Reply text"}},
            "required": ["message_id", "body"]},
    }},
]


# D2: tool-spec grouping so only "core" (file I/O, search, shell, coding,
# save_note) is sent on every request; everything else is a named skill
# loaded on demand via Assistant.load_skill (see LOAD_SKILL_SPEC there),
# to cut the fixed per-request tool-spec overhead this project measured at
# ~2.9k tokens for all specs (DEV_LOG, 2026-09-21) down to just what a given
# conversation actually uses. Lives here, not in assistant.py, since it's
# purely a grouping of THIS module's own tool names/specs.
SKILLS = {
    "desktop": ["open_app", "open_file", "close_app", "list_windows", "close_window", "list_apps"],
    "web": ["open_url", "fetch_url", "web_search"],
    "browser": ["browser_navigate", "browser_read", "browser_click", "browser_type",
                "browser_submit", "browser_close", "browser_screenshot"],
    "gmail": ["gmail_list_messages", "gmail_read_message", "gmail_send_message", "gmail_reply_message"],
    # V2: separate from "browser" deliberately -- analyze_image works on ANY
    # image (a screenshot, a photo, a diagram the user points to), not just
    # a browser_screenshot capture. A browser-screenshot-then-analyze
    # workflow needs both skills loaded; each stays single-purpose.
    "vision": ["analyze_image"],
}
SKILL_DESCRIPTIONS = {
    "desktop": "open/close desktop applications and windows",
    "web": "open a URL, fetch a page's text, search the web",
    "browser": "navigate/read/click/type/submit/screenshot in a real, interactive browser session",
    "gmail": "read, send, and reply to the user's Gmail",
    "vision": "look at an image file and answer a question about it",
}


def core_tool_names() -> set[str]:
    """Every registered tool NOT grouped into a skill above -- the
    always-sent baseline. A tool added to TOOL_FUNCTIONS without being added
    to SKILLS defaults to core (always available) -- the safe direction to
    fail in, since forgetting to categorize a new tool makes it always-on,
    never silently unreachable."""
    skill_tools = {name for names in SKILLS.values() for name in names}
    return set(TOOL_FUNCTIONS) - skill_tools

