# R.A.V.E.N

CLI assistant. Supports two model providers out of the box -- OpenRouter
and NVIDIA's free NIM API (build.nvidia.com) -- switchable in
`~/.raven/settings.json` (`model.provider`) or from the desktop app's
Settings panel; see "Model provider" below. Add another later by writing a
new `LLMProvider` subclass in `raven/llm_provider.py`.

## Setup

Any OS with Python 3.10+. Get an API key from [OpenRouter](https://openrouter.ai) (free models available).

```bash
git clone <repo> && cd R_A_V_E_N
python -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install .                       # installs the `raven` and `raven-server` commands
pip install ".[voice]"              # optional: push-to-talk voice input
raven                               # or: python -m raven.cli
```

No `.env` file or environment variable needed -- the first time you run `raven`,
it asks for your name and, if no key is configured yet, your provider and API
key, then saves them to `~/.raven/settings.json`. Change either later with
`/name` or `/apikey [provider]` (and `/provider [name]` to switch providers).
An `OPENROUTER_API_KEY`/`NVIDIA_API_KEY`/`FREELLMAPI_API_KEY` env var or `.env`
entry still works too, and is used as a fallback when nothing is saved in
settings.json. (Developers: `pip install -e ".[dev]"` gives an editable
install plus the test tools. `pip install -r requirements.txt` still works too.)

## Model provider

Two providers work today:

| | OpenRouter (default) | NVIDIA |
|---|---|---|
| `model.provider` | `"openrouter"` | `"nvidia"` |
| API key env var | `OPENROUTER_API_KEY` | `NVIDIA_API_KEY` |
| Get a free key | [openrouter.ai/keys](https://openrouter.ai/keys) | [build.nvidia.com](https://build.nvidia.com) (any model card's "Get API Key") |
| Fallback models | one request, OpenRouter tries each in order | tried client-side, one HTTP call per candidate |
| Picking a model | type an id into `settings.json` | listable from a key (desktop app) or typed manually |

Switch providers with `/provider nvidia` (CLI) or the desktop app's Settings
→ Model picker -- both also take care of the API key (`/apikey` in the CLI,
the key field under Settings → Model in the desktop app), so neither needs
`~/.raven/settings.json` hand-edited. Picking NVIDIA in the desktop app
loads the models your key can access straight from NVIDIA's API (`list_models`
in `raven/server.py`) into a dropdown; OpenRouter still takes a typed model
id, since there's no listing endpoint wired up for it yet. Whichever you set
here also applies to the CLI, since both read the same `settings.json`.
Switching providers clears `fallbacks` (the old ones would be model ids for
the *other* provider) -- set new ones by hand in `settings.json` if you need them.

## Desktop app

A chat window for the same agent (`raven_ui/`, Electron + React + Tailwind CSS). It **starts the
Python server for you** (`python -m raven.server`, using `py -3` / `python` on Windows,
`python3` / `python` elsewhere -- set `RAVEN_PYTHON` to pick an interpreter), so
`pip install .` above is the only other requirement. If a server is already running
on port 8756 it is reused.

```bash
cd raven_ui
npm install
npm start                # run it in development
npm run make             # build an installer for the OS you're on -> raven_ui/out/make/
```

`npm run make` produces: Windows `Setup.exe` (Squirrel) + zip; macOS `.app` in a zip;
Linux `.deb` and `.rpm`. Installers are **unsigned** for now, so Windows SmartScreen and
macOS Gatekeeper will warn on first launch (code signing needs a paid developer
certificate). Each OS has to be built on that OS -- the `build-desktop` GitHub Action
does all three on a tag or a manual run.

### Cancelling a request

The Send button becomes a **Cancel** button while R.A.V.E.N is working. Clicking it
tells the server to discard whatever answer eventually comes back -- the underlying
network call can't actually be interrupted mid-flight (it runs on a real background
thread), so the button shows "Cancelling..." until that call genuinely finishes, at
which point the half-finished turn is dropped from the conversation (never sent to
the model on a later turn, never shown), the same "roll back, don't pollute later
context" behavior the CLI's own `Ctrl+C` already has. The composer stays disabled
for that whole window, to keep a second message from being sent while the discarded
one might still be mutating the conversation in the background.

### Message actions: copy, edit, resend

Hovering a message reveals small action links below it. Every message gets
**Copy** (the raw text, not the rendered markdown, to the clipboard --
briefly shows "Copied"). A user message additionally gets **Edit** (loads its
exact text into the composer, so you can change it before sending) and
**Resend** (sends the exact same text again as a new message). Neither
touches the existing conversation history -- both always add a NEW message
rather than replacing or rerunning the old one, so nothing you've already
sent is ever silently rewritten. Both disable themselves while a request is
in flight, same guard the Composer's own Send button already has.

### Notifications

R.A.V.E.N sends a native OS notification whenever it needs your confirmation
(a tool call that's about to do something consequential -- deleting a file,
running a shell command, etc.) and again whenever it finishes a turn --
including a reply that's actually a question back to you, since there's no
separate "needs advice" signal, just a normal reply. This works the same in
the desktop app and the CLI (one hook, in `Assistant` itself, covers both),
using `notify-send` on Linux, `osascript` on macOS, and a PowerShell toast on
Windows -- whichever is available; if none is, notifications are silently
skipped rather than erroring. A missed notification never blocks or fails
the actual confirmation/reply it was announcing.

### Voice mode (push-to-talk)

The desktop app's "Voice" mode (left rail) is a second, independent voice
input path from the CLI's `ctrl+v` -- it captures audio in the renderer
(Web Audio API, no ffmpeg) and sends it to `raven/server.py`'s `transcribe`
message, which runs it through the exact same offline faster-whisper
pipeline (`raven/voice.py`) the CLI uses, so the same setup applies:
`pip install ".[voice]"` and (Linux only) `libportaudio2`, plus the one-time
`python -m raven.voice` model download.

Hold **Shift** to record, release to stop. By default the transcript is
shown for you to edit and explicitly Send or Discard -- never auto-sent,
same posture as `ctrl+v`. Check "Send automatically on release" (top-right
of the Voice stage) to skip review and send straight to the model instead;
this is remembered across restarts (browser `localStorage`). Recording is
disabled while disconnected or while a request is already in flight, and
releasing Shift before the microphone finishes opening (a very fast tap) is
handled cleanly rather than getting stuck. Electron grants microphone
access to the app automatically (`main.ts`'s permission handler) -- only
this window can ask.

### Design mode: a mathematical drawing playground

The desktop app's "Designer" mode (left rail) is a Figma-like canvas where
R.A.V.E.N draws shapes using mathematical generators -- golden rectangles and
spirals, regular/star polygons, radial (mandala-style) patterns, grid
layouts, and Bezier/Lissajous curves -- and you can then directly move,
resize, rotate, recolor, reparametrize, reorder, group/ungroup, and delete
what she drew, with pan/zoom and undo/redo (Ctrl+Z / Ctrl+Shift+Z). Ask for
something the same way you'd chat: "draw a hexagon," "make it red," "turn
that into a mandala with 8 copies."

Every shape's actual geometry is generated server-side from its own
mathematical parameters (e.g. a hexagon's `sides`/`radius`) -- editing a
shape's numbers in the Properties panel regenerates it exactly, rather than
just stretching a picture of it. Your own edits are also remembered: R.A.V.E.N
gets a short note about what you changed by hand before her next reply, so
"make it bigger" after you've dragged a shape somewhere new still means the
right shape. The canvas is saved per chat session, exactly like the
conversation itself -- switching chats switches canvases too.

## Headless mode

```bash
raven -p "What is 2+2?"          # runs one request, prints the plain-text reply, exits
raven -p "Delete old.log" -y     # -y auto-approves confirmation-gated tool calls
```

No TTY needed — for scripting, cron, or piping into other tools. Without
`-y`, anything that would need your confirmation (`delete_file`,
`run_command`, etc.) is denied by default, never silently approved just
because no one's watching; the reply says so, and R.A.V.E.N exits normally.
On an actual error, it prints the error and exits with status 1. History,
settings, and `RAVEN.md` rules all work the same as interactive mode.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Runs in a few seconds, no network and no real model or account touched by
default. Everything that needs those (a live OpenRouter call, a real Gmail
account) is marked `live` and skipped unless you ask for it explicitly:

```bash
pytest -m live
```

## Project/user rules

Drop a `RAVEN.md` in a project's root and R.A.V.E.N will load it into the
system prompt on startup — coding standards, how to run tests, whatever the
model should know about that project specifically. A `~/.raven/RAVEN.md`
does the same thing globally, across every project. Both are optional; if
present, R.A.V.E.N says so on startup ("Loaded rules from ..."). User rules
come first, project rules last — closer to the actual task, so they win on
anything the two disagree on. Each file is capped at ~4000 characters (same
truncation convention as everything else in this project).

## Code editing and search

For working in a codebase: `tree` shows a directory's structure recursively
(bounded depth/entry count, so it stays usable on a big project); `glob_files`
finds files by name pattern (`**/*.py`); `grep` searches file contents by
regex; `edit_file` makes a targeted change to part of an existing file
instead of rewriting the whole thing with `write_file`. `edit_file` requires
the text being replaced (`old_string`) to match **exactly once** in the
file — no match, or more than one, and it refuses rather than guess which
occurrence was meant; add more surrounding context to make it unique. Unlike
`write_file` (which only confirms when overwriting something that already
exists), `edit_file` always confirms, since it only ever touches an existing
file — the confirmation shows exactly what's being replaced and with what.

`run_tests` (default command: `pytest`) is a dedicated tool for running a
project's actual test suite — same confirmation policy as `run_command`, but
with a 120s timeout instead of 30s, since a real test suite (this project's
own included) can legitimately take longer than a quick shell command.

## Permissions

By default, `run_command`/`run_tests` confirm unless the command is a plain
call to a built-in read-only program (`ls`, `cat`, etc.), and `git` confirms
unless the subcommand is `status`/`log`/`diff`/`show`. `~/.raven/settings.json`
lets you extend that trust — additively, never in place of it:

```json
{
  "permissions": {
    "allow_commands": ["pytest"],
    "allow_git": ["commit", "add"]
  }
}
```

`allow_commands` lets a plain, metacharacter-free call to that program skip
confirmation too (via `run_command` or `run_tests`); `allow_git` does the
same for a git subcommand. The shell-metacharacter check always still
applies regardless: `"pytest; rm -rf ~"` still confirms even with `pytest`
trusted — trusting a program name only ever exempts a plain call to exactly
that program, never anything chained after it.

### `sudo` commands (interactive CLI only)

`run_command`/`run_tests` normally run with stdin closed (`DEVNULL`) so a
command that tries to read input can't hang the whole session on your real
terminal — but that also meant a `sudo` command could never actually
authenticate, since it had no way to read a password even after you approved
it. In the interactive CLI, a `sudo ...` command first tries non-interactively
(`sudo -n`), so it never asks for anything if `sudo`'s own cached credential
(its normal ~15 minute ticket) is still valid; only if that fails because a
password is actually needed does it ask you for one (masked input, used once,
never logged, never sent to the model). Approving the command in the usual
`Allow?` prompt is still required first — this doesn't loosen or skip that;
it just fixes what happens after you say yes. The actual authenticated
command gets a generous 5-minute timeout (a real privileged command like a
package install can easily take longer than a quick shell command), and if
it does time out, the whole process is killed outright rather than left
running in the background — an earlier version of this could leave an
orphaned `sudo` process attached to your terminal, which could then prompt
for a password on its own, outside R.A.V.E.N's control. Headless mode (`-p`)
never prompts for a sudo password (no guaranteed terminal to safely ask on),
so a `sudo` call there fails cleanly exactly as before if it needs one. The
desktop app doesn't prompt for one either, for the same reason.

## Hooks

`~/.raven/settings.json` can also run a shell command automatically before
and/or after a specific tool call — e.g. auto-formatting a file right after
R.A.V.E.N edits it:

```json
{
  "hooks": {
    "post": {"edit_file": "black {path}"},
    "pre": {"edit_file": "cp {path} {path}.bak"}
  }
}
```

`{placeholder}`s are filled in from that tool call's own arguments (`{path}`,
or anything else the tool takes). Hooks never ask for confirmation — putting
one in this file, which only you control, **is** the approval, the same
trust boundary as `permissions` above. A `post` hook only runs if the tool
call actually succeeded (not after `edit_file`'s own "not found" refusal,
which changed nothing); a hook's own output or error is appended to the
tool's result so you can see it happened. Scoped to the model's own
tool-calling loop — manual `/write`, `/run`, etc. don't trigger hooks.

## Skills (on-demand tools)

Only file/shell/coding tools (`read_file`, `edit_file`, `grep`, `run_command`,
`git`, `save_note`, etc.) are sent to the model on every request. Desktop app
control, the web, R.A.V.E.N's own browser, Gmail, and vision are
grouped into named **skills** — `desktop`, `web`, `browser`, `gmail`, `vision`
— that the model unlocks itself with a `load_skill` call the moment a request
actually needs one, then uses like any other tool for the rest of that
session. `/skills` shows which are currently loaded. This cut R.A.V.E.N's
fixed per-request overhead (system prompt + every tool's spec, sent
regardless of what a conversation is actually about) by roughly half —
measured at ~5.6k tokens down to ~2.75k when no skill is loaded, since
neither the unused tool specs nor their guardrail instructions (e.g. the
Gmail Reply-To/untrusted-content guidance, the browser submit-safety rule)
are paid for until they're relevant. Nothing about how a loaded skill's
tools behave changes — same confirmation policy, same everything — this only
affects when their specs and instructions actually reach the model. A fresh
session always starts back at core-only; there's no persistence to manage.

## Voice input

Press `ctrl+v` at the prompt to start recording, `ctrl+v` again to stop —
transcribed locally and offline via [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
(no OpenRouter quota used, no audio sent anywhere) and inserted into the
input buffer for you to review, edit, or discard, exactly like anything
you'd typed — it never auto-sends. The bottom toolbar shows `[ctrl+v: voice
input]` when ready and `[ctrl+v: recording — press again to stop]` while
recording.

Needs two optional Python packages plus, **on Linux specifically**, a system
library:

```bash
pip install sounddevice faster-whisper
sudo apt install libportaudio2   # Debian/Ubuntu and derivatives (Zorin, Mint, ...)
# Fedora:  sudo dnf install portaudio
# Arch:    sudo pacman -S portaudio
```

That last step is a real, unavoidable requirement of the `sounddevice`
package on Linux — its wheel doesn't bundle the PortAudio binary there the
way it does on Windows/macOS, so without the system library it fails to
import with `OSError: PortAudio library not found`. None of this is required
to run R.A.V.E.N at all: without it, `ctrl+v` just inserts a
"voice input not set up" message instead of recording, and everything else
works exactly the same.

**One-time model download (explicit, never a side effect of a keypress):**

```bash
python -m raven.voice        # downloads the Whisper "base" model (~140MB), resumable
```

Until that's done, `ctrl+v` just shows "voice model not downloaded — run:
python -m raven.voice" in the toolbar. If the download makes no progress
(0-byte `.incomplete` files under `~/.cache/huggingface/hub`), retry with
`HF_HUB_DISABLE_XET=1 python -m raven.voice` — the newer Hugging Face "xet"
transfer path stalls on some networks. Transcription runs in a background
thread, so the prompt stays responsive; `ctrl+c` at the prompt clears the
line (it doesn't crash), `ctrl+d` on an empty prompt exits. Note `ctrl+v`
is bound to voice, so it does not paste — use your terminal's paste
shortcut (usually `ctrl+shift+v`).

## Vision (image analysis)

`analyze_image` (in the `vision` skill) looks at an image file — a
screenshot, a photo, a diagram — and answers a question about it, using a
**separate**, configurable model (`settings.json`'s `model.vision`) since
the main chat model isn't necessarily vision-capable. `browser_screenshot`
(in the `browser` skill) saves a PNG of the current page in R.A.V.E.N's own
browser session for `analyze_image` to look at — the two compose rather than
being one merged tool, same as `grep`/`glob_files`. No confirmation needed
for either (read-only). Capturing a screenshot of your actual desktop
(rather than just R.A.V.E.N's own browser page) isn't supported yet — it
needs the Wayland/GNOME screenshot portal's more involved async permission
flow, a deliberate scope decision, not an oversight.

## Structure

```
raven/
  llm_provider.py   # LLM interface + Provider (Openrouter) implementation, RAVEN.md loading
  assistant.py       # conversation state, calls the provider
  tools.py           # file I/O + shell/search primitives, tool specs
  browser.py          # Playwright-backed browser session (own thread)
  gmail.py            # Gmail OAuth, list/read/send/reply
  store.py            # SQLite persistence for conversation history
  statusline.py       # bottom status-line segments
  config.py           # env/config loading + settings.json
  cli.py               # entry point, input/output loop
tests/                 # pytest suite — see "Tests" above
```

## Memory

Conversation history is persisted to `~/.raven/history.db` (SQLite) and
reloaded on startup, so context survives between runs.

This can go stale: if system state changes between turns (an app gets
installed, a window closes) an old tool result sitting in history can get
treated as still true. R.A.V.E.N is told to re-check rather than trust a
stale "X isn't available" from earlier in the conversation, but if it still
seems to be working from old information, run `/forget` to clear history
(in memory and on disk) and start fresh.

Only a bounded window of recent history is actually sent to the model each
request (see "Status line" below for the budget). Once a conversation grows
past that, older turns aren't just dropped — they're summarized into a
running summary that's sent in their place, so facts and decisions from
earlier in a long conversation are still available, just compressed. This
happens automatically whenever it's needed, or on demand with `/compact`.
The summary itself is never written to disk — the full raw conversation in
`~/.raven/history.db` is untouched either way, so nothing is ever actually
lost, and a fresh session always starts from the real thing.

Separately, R.A.V.E.N can save short, durable facts to
`~/.raven/memory/notes.md` with its own `save_note` tool — a stated
preference, a project convention, a correction — loaded into every future
session automatically, so they survive `/forget` and outlive any single
conversation. It decides on its own when something's worth remembering; it
never needs confirmation, since it's an append-only, bounded write (one note
capped at ~500 characters, the whole file at ~4000 — past that it refuses
rather than silently truncating a fact or evicting an older one). `/notes`
shows what's currently saved; the file is a plain markdown list you can edit
or clear by hand at any time.

## Status line

The prompt shows a bottom status line. Segments and their order come from
`~/.raven/settings.json` (optional):

```json
{
  "model": {
    "name": "nvidia/nemotron-3.5-lightning:free",
    "fallbacks": ["nex-agi/nex-n2.5-pro:free", "openrouter/free"]
  },
  "statusline": { "segments": ["model", "context", "tokens", "memory", "cpu"] }
}
```

Status-line segments: `model` (what actually answered), `context` (approx.
tokens of history sent to the model / the budget), `tokens` (session total),
`memory`, `cpu`. Add your own in `raven/statusline.py` — one function per
segment.

Change it from inside R.A.V.E.N with `/statusline` (no args lists the segments;
`/statusline model tokens cpu` sets and saves them).

`model.fallbacks` are tried in order when the primary free model is down or
rate-limited (OpenRouter's `models` routing).

## Platform support

| | Linux | Windows | macOS |
|---|---|---|---|
| Files, search, edit, git, web, browser, Gmail, voice, terminal + desktop UI | yes | written to be portable, **not yet run** | written to be portable, **not yet run** |
| App and window control (`open_app`, `close_app`, `list_windows`, ...) | yes (window control needs GNOME) | written + unit-tested, **not yet run on real Windows** (Start Menu apps, PowerShell windows) | written + unit-tested, **not yet run on a real Mac** (`.app` bundles, AppleScript; windows need Accessibility permission) |
| Read-only command auto-approval | Unix commands | `dir`, `type`, `where`, `findstr`, ... | Unix commands |

OS-specific code lives in `raven/platforms/`. Testers: `WINDOWS_TEST_CHECKLIST.md`,
`MACOS_TEST_CHECKLIST.md`.

## Opening applications (Linux)

Ask in natural language ("open Firefox", "launch the file manager") and
R.A.V.E.N resolves the name against your installed `.desktop` entries —
covering native packages, Flatpak, and Snap installs — and launches it with
`gtk-launch` — no confirmation prompt, since it's not a destructive action.
Ambiguous or unknown names get you a candidate list instead of a guess.

To open a specific *file*, R.A.V.E.N uses `open_file`: with no app given it
opens with the system default handler (`xdg-open`); with an app name, it
matches it the same way as `open_app` and passes the file to `gtk-launch` as
the file/URI argument. Ask it to create a file and open it, and it just calls
`write_file` then `open_file`.

App-name matching is whole-word, both directions ("text editor" matches
"the text editor app" and vice versa) so generic phrasing works without
guessing a brand. If nothing matches, it's offered `list_apps` — every
installed app's display name — instead of picking a plausible-sounding one
from general knowledge.

`close_app` closes a running app by signalling its process — this closes
**every** window of that app at once, and always asks for confirmation first
since it can lose unsaved work.

To close just *one* window of a multi-window app (e.g. one of several VS Code
or browser windows), use `list_windows` / `close_window` instead. These need
the [Window Calls](https://github.com/ickyicky/window-calls) GNOME Shell
extension (`window-calls@domandoman.xyz`) — plain X11 tools like `wmctrl`
can't see app windows at all on GNOME/Wayland, since they're native Wayland
surfaces with no X11 proxy. Install it, then:

```bash
gnome-extensions install window-calls@domandoman.xyz.shell-extension.zip
# log out and back in — GNOME Shell only rescans extensions on Wayland at login
gnome-extensions enable window-calls@domandoman.xyz
```

`close_window` matches a window by a substring of its title (see
`list_windows` for exact titles) and closes only that one, gracefully (Mutter's
own close, same as clicking the × button) — the other windows of the same app
are untouched.

## Web access

- `open_url` — opens a URL in the default browser, or a specific named one
  (e.g. "open figma.com in Firefox" when Firefox isn't your default).
- `fetch_url` — fetches a page and returns its text (HTML stripped, truncated).
- `web_search` — searches the web via DuckDuckGo's HTML results (no API key
  needed). This is a free, keyless, and therefore rate-limited endpoint — it
  can start returning a "temporarily rate-limited" message after a burst of
  searches. There's no paid fallback configured; if that's too flaky in
  practice, the fix is a real search API key, not more scraping tricks.

## Browser interaction

For pages that need JavaScript, or that R.A.V.E.N needs to click/type on,
`browser_navigate` / `browser_read` / `browser_click` / `browser_type` /
`browser_submit` / `browser_close` drive a real, persistent browser session
via [Playwright](https://playwright.dev) — `pip install playwright` plus a
one-time `playwright install chromium` (downloads a ~115MB browser binary).

- **Isolated**: a fresh Chromium session with none of your real browser's
  cookies or logins — it can read and interact with public pages, but can't
  act as you on sites you're signed into elsewhere.
- **Headless**: runs invisibly, no window pops up on your desktop. Its
  status-line entries and replies are how you see what it's doing.
- **Text-based selectors**: click/type target visible text or labels, not
  CSS selectors, since the model can act on what it reads without needing
  raw HTML.
- **Submitting requires confirmation, and it's enforced, not just asked
  for**: `browser_click` inspects the DOM before clicking — a real HTML
  submit control (a `<button>`/`<input type=submit>`, or a bare `<button>`
  inside a `<form>`) gets refused and redirected to `browser_submit`, which
  goes through the same confirmation gate as `run_command`/`delete_file`.
  This is a structural check, not model judgment — but it only covers real
  HTML submit semantics. A JS-driven button that commits an action without
  being a literal submit control (e.g. a script-handled "Buy Now") isn't
  caught by it; R.A.V.E.N is instructed to use `browser_submit` itself for
  anything that commits, but that part relies on the model getting it right,
  same as other soft boundaries in this project (`open_app` vs `open_file`).

## Gmail access

`gmail_list_messages` / `gmail_read_message` let R.A.V.E.N read your real
Gmail. `gmail_send_message` sends a **new** email as you, and
`gmail_reply_message` replies to an existing one — in its own thread, to
whoever the original says to reply to (its `Reply-To`, else its `From`). Both
need **your confirmation** each time, and the prompt shows the full
To/Subject/Body, the way `run_command` shows the full command.

For a reply the recipient is worked out from the original message, so the
prompt shows the *resolved* address rather than the visible sender. That
matters: a sender can set `Reply-To` to point replies somewhere else, and you
should see that before approving. It's plain reply only — no reply-all.

The OAuth scopes granted are exactly `gmail.readonly` + `gmail.send` —
deliberately not `gmail.modify`, so even with a code bug, Google's API itself
refuses to delete or alter existing mail. (Replying needs no extra scope: a
reply is just a send carrying a thread id and `In-Reply-To`/`References`
headers, so it's *our* tool boundary, not Google's, that limits R.A.V.E.N to
plain replies.) A recipient containing a line break is refused outright, since
that could smuggle an extra recipient in.

This needs a one-time setup only you can do (it requires your Google account
and browser-based consent):

1. Go to the [Google Cloud Console](https://console.cloud.google.com/),
   create a project (or use an existing one).
2. Enable the **Gmail API** for it (APIs & Services → Library → search
   "Gmail API" → Enable).
3. Configure the **OAuth consent screen** (APIs & Services → OAuth consent
   screen). "External" + "Testing" mode is fine for personal use — add your
   own Google account under "Test users".
4. Create credentials: APIs & Services → Credentials → Create Credentials →
   OAuth client ID → Application type **Desktop app**.
5. Download the resulting JSON and save it as `~/.raven/gmail_credentials.json`.
6. Ask R.A.V.E.N to do something with Gmail. The first call opens a browser
   for you to log in and grant access (read + send); after that, a cached
   token at `~/.raven/gmail_token.json` covers every run until it's revoked
   or expires — no repeated logins.

Until step 5 is done, Gmail tools return a clear "not set up yet" message
instead of an error or a guess. If you'd already completed setup before
`gmail_send_message` was added, the cached token only has the read scope —
delete `~/.raven/gmail_token.json` and go through step 6 again to re-grant
with send included.

## Swapping to a local model later

Add e.g. `OllamaProvider(LLMProvider)` in `llm_provider.py`, then in
`cli.py` change one line: `provider = OllamaProvider(...)`.
Nothing else in the codebase changes.


