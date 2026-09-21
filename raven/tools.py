"""Execution primitives: file I/O and shell commands."""
import ast
import difflib
import html
import json
import re
import shutil
import subprocess
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from raven import browser as _browser
from raven import gmail as _gmail

APP_DIRS = [
    Path("/usr/share/applications"),
    Path("/usr/local/share/applications"),
    Path.home() / ".local/share/applications",
    Path("/var/lib/flatpak/exports/share/applications"),  # system-wide flatpak installs
    Path.home() / ".local/share/flatpak/exports/share/applications",  # per-user flatpak installs
    Path("/var/lib/snapd/desktop/applications"),  # snap installs
]

_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; RAVEN-agent/1.0)"}


def resolve(path: str) -> Path:
    return Path(path).expanduser().resolve()


def read_file(path: str) -> str:
    return resolve(path).read_text()


def write_file(path: str, content: str = "") -> str:
    resolve(path).write_text(content)
    return f"Wrote {path}"


def make_dir(path: str) -> str:
    resolve(path).mkdir(parents=True, exist_ok=True)
    return f"Created {path}"


def list_dir(path: str = ".") -> str:
    entries = sorted(resolve(path).iterdir())
    return "\n".join(f"{'d' if e.is_dir() else '-'} {e.name}" for e in entries) or "(empty)"


def delete_file(path: str) -> str:
    resolve(path).unlink()
    return f"Deleted {path}"


def run_command(command: str) -> str:
    result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
    output = result.stdout + result.stderr
    return output.strip() or f"(exited {result.returncode}, no output)"


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


def _installed_apps() -> dict[str, str]:
    """Lowercase app display name -> .desktop id, from standard app directories."""
    apps = {}
    for directory in APP_DIRS:
        if not directory.is_dir():
            continue
        for desktop_file in directory.glob("*.desktop"):
            try:
                lines = desktop_file.read_text(errors="ignore").splitlines()
            except OSError:
                continue
            name, no_display, sections_seen = None, False, 0
            for line in lines:
                if line.startswith("["):
                    sections_seen += 1
                    if sections_seen > 1:  # left the [Desktop Entry] section
                        break
                elif line.startswith("Name=") and name is None:
                    name = line.split("=", 1)[1].strip()
                elif line.strip() == "NoDisplay=true":
                    no_display = True
            if name and not no_display:
                apps[name.lower()] = desktop_file.stem
    return apps


def list_apps() -> str:
    """List every installed application's display name, so the caller can pick
    the exact one to pass to open_app/open_file instead of guessing."""
    apps = _installed_apps()
    return "\n".join(sorted(n.title() for n in apps)) or "(no apps found)"


def _contains_words(haystack: str, needle: str) -> bool:
    """True if every word of `needle` appears as a whole word in `haystack`
    (word-boundary, not raw substring — a one-letter app name like "R" would
    otherwise match almost any query just by appearing inside another word)."""
    haystack_words = set(re.findall(r"[a-z0-9]+", haystack))
    needle_words = re.findall(r"[a-z0-9]+", needle)
    return bool(needle_words) and all(w in haystack_words for w in needle_words)


def _match_app(name: str) -> tuple[str, str] | str:
    """Resolve a (fuzzy) app name to (display name, .desktop id), or an error string."""
    apps = _installed_apps()
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


def _binary_name(app_id: str) -> str | None:
    """The executable name from an app's Exec= line (its process name, for closing it)."""
    for directory in APP_DIRS:
        desktop_file = directory / f"{app_id}.desktop"
        if not desktop_file.is_file():
            continue
        try:
            for line in desktop_file.read_text(errors="ignore").splitlines():
                if line.startswith("Exec="):
                    cmd = line.split("=", 1)[1].strip().split()
                    if not cmd:
                        return None
                    name = Path(cmd[0]).name
                    if name == "flatpak":
                        # Exec is "flatpak run ... --command=<real binary> ... <app-id> ...",
                        # so the naive first token is always "flatpak" — useless for pkill,
                        # since it'd match every running flatpak app, not just this one.
                        # Confirmed against a real running process: --command's value is
                        # exactly what shows up in `ps` (e.g. --command=firefox -> the
                        # actual /app/lib/firefox/firefox process). Fall back to the
                        # app-id itself if --command= isn't present.
                        command_arg = next(
                            (a[len("--command="):] for a in cmd if a.startswith("--command=")),
                            None,
                        )
                        return command_arg or app_id
                    return name
        except OSError:
            continue
    return None


# Some single-instance apps default their CLI to opening a brand-new window per
# invocation rather than reusing the one already open. Extra args listed here
# (keyed by .desktop id) fix that, so R.A.V.E.N opening a file on your behalf
# doesn't pile up windows. Add an entry here if another app needs the same fix.
REUSE_WINDOW_ARGS = {
    "code": ["-r"],  # VS Code: -r/--reuse-window, opens into the last active window
}


def _gtk_launch(app_id: str, *args: str) -> None:
    subprocess.Popen(
        ["gtk-launch", app_id, *args],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def open_app(name: str) -> str:
    """Launch an installed desktop application by (fuzzy) name."""
    match = _match_app(name)
    if isinstance(match, str):
        return match
    if not shutil.which("gtk-launch"):
        return "Can't launch apps: gtk-launch is not installed."
    matched_name, app_id = match
    _gtk_launch(app_id)
    return f"Launched {matched_name.title()}."


def _launch_app_with_target(app: str, target: str, label: str) -> str:
    """Resolve `app` (fuzzy name) and launch it with `target` (a file path or
    URL) as its argument. `label` is what the success message shows. Shared by
    open_file and open_url's app= path."""
    match = _match_app(app)
    if isinstance(match, str):
        return match
    matched_name, app_id = match
    extra_args = REUSE_WINDOW_ARGS.get(app_id)
    binary = _binary_name(app_id) if extra_args else None
    if extra_args and binary and shutil.which(binary):
        # Bypass gtk-launch's %F template substitution here: it's undocumented
        # whether a non-file flag like "-r" survives that substitution alongside
        # the target, so invoke the real binary directly for a guaranteed argv.
        subprocess.Popen(
            [binary, *extra_args, target],
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return f"Opened {label} in {matched_name.title()}."
    if not shutil.which("gtk-launch"):
        return "Can't launch apps: gtk-launch is not installed."
    _gtk_launch(app_id, target)
    return f"Opened {label} in {matched_name.title()}."


def open_file(path: str, app: str = "") -> str:
    """Open a file, either with the system default app or a named one."""
    target = resolve(path)
    if not target.exists():
        return f"No such file: {path}"
    if not app:
        if not shutil.which("xdg-open"):
            return "Can't open files: xdg-open is not installed."
        subprocess.Popen(
            ["xdg-open", str(target)],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return f"Opened {path} with the default app."
    return _launch_app_with_target(app, str(target), path)


def close_app(name: str) -> str:
    """Close a running application by (fuzzy) name. Requires user confirmation.

    This closes by process signal (pkill -f <binary>), so it ALWAYS closes every
    window of the app at once. To close just one window, use close_window
    instead (needs the "Window Calls" GNOME Shell extension — see its docstring)."""
    match = _match_app(name)
    if isinstance(match, str):
        return match
    matched_name, app_id = match
    binary = _binary_name(app_id)
    if not binary:
        return f"Couldn't determine the process for {matched_name.title()}."
    result = subprocess.run(["pkill", "-f", binary], capture_output=True, text=True)
    if result.returncode == 0:
        return f"Closed {matched_name.title()} (all of its windows)."
    if result.returncode == 1:
        return f"{matched_name.title()} does not appear to be running."
    return f"Couldn't close {matched_name.title()}: {result.stderr.strip() or 'pkill error'}"


def _window_calls(method: str, *args: str) -> str:
    """Call a method on the "Window Calls" GNOME Shell extension's D-Bus
    interface (org.gnome.Shell.Extensions.Windows) and return its raw string
    result. Raises RuntimeError with a clear cause if the extension isn't
    installed/enabled or the call otherwise fails."""
    if not shutil.which("gdbus"):
        raise RuntimeError("gdbus is not installed")
    result = subprocess.run(
        ["gdbus", "call", "--session", "--dest", "org.gnome.Shell",
         "--object-path", "/org/gnome/Shell/Extensions/Windows",
         "--method", f"org.gnome.Shell.Extensions.Windows.{method}", *args],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        err = result.stderr.strip()
        if "org.freedesktop.DBus.Error.UnknownMethod" in err or "No such interface" in err:
            raise RuntimeError('the "Window Calls" GNOME Shell extension is not installed/enabled')
        raise RuntimeError(err or "gdbus call failed")
    # gdbus prints a Python-tuple-literal, e.g. ('the returned string',) for a
    # method with a return value, or () for a void one like Close.
    parsed = ast.literal_eval(result.stdout.strip())
    return parsed[0] if parsed else ""


def list_windows() -> str:
    """List every open window: id, owning app, and title. Use this to find a
    window's id/title before calling close_window on it. Requires the "Window
    Calls" GNOME Shell extension (github.com/ickyicky/window-calls); if it's
    not installed/enabled, says so plainly instead of failing silently."""
    try:
        windows = json.loads(_window_calls("List"))
    except (RuntimeError, subprocess.TimeoutExpired, ValueError) as e:
        return f"Can't list windows: {e}."
    if not windows:
        return "(no windows found)"
    return "\n".join(
        f"{w.get('id')} | {w.get('wm_class', '?')} | {w.get('title', '')}" for w in windows
    )


def close_window(title: str) -> str:
    """Close exactly one window, matched by a case-insensitive substring of its
    title (see list_windows for the exact titles). Requires user confirmation.
    Unlike close_app (which always closes an app's every window), this closes
    only the one matching window — a real per-window close, using the "Window
    Calls" GNOME Shell extension. If it's not installed/enabled, says so."""
    try:
        windows = json.loads(_window_calls("List"))
    except (RuntimeError, subprocess.TimeoutExpired, ValueError) as e:
        return f"Can't close window: {e}."
    query = title.strip().lower()
    matches = [w for w in windows if query in (w.get("title") or "").lower()]
    if not matches:
        return f"No open window matches '{title}'. Call list_windows to see what's open."
    if len(matches) > 1:
        candidates = "; ".join(f"{w.get('id')}: {w.get('title')}" for w in matches[:10])
        return f"Multiple windows match '{title}': {candidates}. Be more specific."
    win = matches[0]
    try:
        _window_calls("Close", str(win["id"]))
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        return f"Couldn't close window: {e}."
    return f"Closed window: {win.get('title')}"


TOOL_FUNCTIONS = {
    "read_file": read_file,
    "write_file": write_file,
    "make_dir": make_dir,
    "list_dir": list_dir,
    "delete_file": delete_file,
    "run_command": run_command,
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
    "gmail_list_messages": _gmail.list_messages,
    "gmail_read_message": _gmail.read_message,
    "gmail_send_message": _gmail.send_message,
    "gmail_reply_message": _gmail.reply_message,
}

TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read and return the contents of a text file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Path to the file"}},
            "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Write (or overwrite) a text file with the given content.",
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
        "name": "list_dir",
        "description": "List the entries of a directory (d = dir, - = file).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory path (default: current dir)"}}},
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
        "description": "Run a shell command and return its output. Requires user confirmation, "
                       "except for a plain (no chaining/redirection) call to a read-only command "
                       "like ls, cat, ps, pwd, wc, head, tail, which, id, date, uname, df, du, "
                       "hostname, free, uptime, whoami, or echo, which runs immediately.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "Shell command to run"}},
            "required": ["command"]},
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

