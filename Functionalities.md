# R.A.V.E.N — Functionalities

Everything R.A.V.E.N can do today, checked against the code on 2026-09-21
(tool roster, slash commands, settings and confirmation rules were read from
the source, not recalled). For *why* things are the way they are, see
`DEV_LOG.md`; for setup, see `README.md`.

R.A.V.E.N is a terminal assistant that talks to an LLM through OpenRouter and
lets it act on your machine through a fixed set of tools. The model decides
which tool to call; anything consequential goes through a confirmation prompt
first.

**Contents:** [Interface](#interface) · [Agent tools](#agent-tools) ·
[Confirmation model](#confirmation-model) · [Memory and context](#memory-and-context) ·
[Model and configuration](#model-and-configuration) · [Prerequisites](#prerequisites) ·
[Known limitations](#known-limitations) · [Verification notes](#verification-notes) ·
[Where things live](#where-things-live)

---

## Interface

### Slash commands

Typed at the `›` prompt. They run directly, without the LLM. Tab-completion
suggests them as you type `/`.

| Command | What it does | Confirms? |
|---|---|---|
| `/help` | Show the command table | — |
| `/status` | Show CPU, memory and disk usage | — |
| `/read <path>` | Print a file's contents | no |
| `/write <path> <content>` | Write content to a file (overwrites) | **no** |
| `/mkdir <path>` | Create a directory | no |
| `/run <command>` | Run a shell command | **always** |
| `/statusline [segments]` | With no arguments, list segments and the current order; with names, set and save them | — |
| `/forget` | Clear the current chat's history, in memory and on disk (other chats are untouched) | no |
| `/compact` | Summarize older history into a running summary now, freeing up context budget | no |
| `/notes` | Show saved cross-session notes (`~/.raven/memory/notes.md`) | — |
| `/exit`, `/quit` | Leave (plain `exit` / `quit` also work) | — |

Anything that isn't a slash command goes to the model.

### Headless mode

`raven -p "<prompt>"` runs one request non-interactively and exits — no TTY,
no Rich styling, plain text on stdout, so it's pipeable and scriptable
(cron, CI, shell scripts). Confirmation-gated tool calls are **denied by
default** in this mode — headless never silently approves something
consequential just because no one's watching — add `-y`/`--yes` to
auto-approve them instead. History, settings, and `RAVEN.md` rules are
shared with interactive mode; a real error prints and exits with status 1.

### Keys

| Key | Effect |
|---|---|
| `Ctrl+C` | Cancel the current request. The half-finished turn is rolled back so it can't confuse the next one. |
| `Ctrl+O` | Show or hide the model's reasoning trace for the last reply (collapsed by default). It lives in the bottom toolbar, so it toggles instantly with no new reply needed. |
| `Ctrl+V` | Toggle push-to-talk voice input — press to start recording, press again to stop, transcribe locally, and insert the text into the input buffer for review (never auto-sent). Needs optional packages; see "Voice input" below. Silently does nothing useful (inserts a "not set up" note) if they're missing. |

### While it works and after it replies

- **Live activity line.** A custom animated spinner alongside status text
  that shows what the agent is doing right now ("Reading x.txt...",
  "Searching the web for ...", "Opening figma.com in Firefox..."), changing
  as it moves between tool calls. While waiting on the model with no tool
  running, the text instead rotates through a set of idle words every 5s,
  each rendered with a brightness "shimmer" sweeping across it. The moment a
  tool call starts, that rotation pauses — a tool's own status sticks until
  either another tool call replaces it or the turn ends, rather than being
  overwritten by the next random word up to 5s later (a real bug, fixed
  2026-09-23).
- **"Thought for X.Xs".** Printed after each successful reply, with a hint to
  press `Ctrl+O` when the model supplied a reasoning trace. Skipped when the
  request was cancelled or errored.
- **Reasoning trace.** Shown in the bottom toolbar when expanded, capped at
  1200 characters.
- **Markdown rendering** of replies.
- **Startup.** A banner, a randomly chosen greeting, a "Resumed N messages
  from earlier sessions" line if history was loaded, and a "Loaded rules
  from ..." line if a `RAVEN.md` was found (see "Project/user rules" below).

### Bottom status line

Always visible under the prompt. Segments, in the order set by `/statusline`
(default is the first four):

| Segment | Shows |
|---|---|
| `model` | The model that *actually answered* the last request (which can differ from the one requested, if a fallback took over) |
| `context` | History messages sent to the model / the cap (`ctx 12/40`) |
| `tokens` | Cumulative tokens used this session |
| `memory` | System memory in use |
| `cpu` | System CPU load |

It also shows `[ctrl+o: thoughts collapsed|expanded]` and refreshes every 2 s.
New segments are one function in `raven/statusline.py`.

---

## Agent tools

The model has 35 tools (34 registered + `load_skill` itself), but only the
ones in "Files and shell" below (plus `load_skill`) are sent on every
request — everything under "Applications and windows", "Web", "Browser
interaction", "Vision", and "Gmail" is grouped into a named **skill**
(`desktop`, `web`, `browser`, `vision`, `gmail` respectively) that stays
locked until the model calls `load_skill` for it, then works exactly like
any other tool for the rest of that session (see "Skills" below for why).
"Confirms" means you are asked to allow it before it runs; the prompt is
built from what is about to happen.

### Files and shell

| Tool | What it does | Confirms? |
|---|---|---|
| `read_file` | Read a text file | no |
| `write_file` | Write a text file | only if it already exists (overwriting) |
| `edit_file` | Replace an exact, unique string in an existing file — a targeted change without rewriting the whole thing | **yes, always** (it only ever touches an existing file) |
| `make_dir` | Create a directory, parents included | no |
| `list_dir` | List a directory (`d` = dir, `-` = file) | no |
| `tree` | Recursive directory structure, indented by depth (bounded to 3 levels / 500 entries) | no |
| `glob_files` | Find files by name/glob pattern, e.g. `**/*.py` (for content, use grep) | no |
| `grep` | Search text files by regex (`file:line:` results) | no |
| `save_note` | Save one fact to durable, cross-session memory (see "Memory and context") | no |
| `delete_file` | Delete a single file (not directories) | yes |
| `run_command` | Run a shell command, 30 s timeout | yes, **unless** it is a plain call to a read-only program (below) |
| `run_tests` | Run a test command (default `pytest`), 120 s timeout | same policy as `run_command` |
| `git` | Run a git subcommand in the current repo | yes, unless read-only (`status`, `log`, `diff`, `show`) |

**`edit_file`'s uniqueness rule:** `old_string` must match exactly once in
the file — zero matches or more than one both refuse outright rather than
guess, so a vague target (e.g. a common one-line string that appears twice)
never risks editing the wrong spot. The model is expected to include enough
surrounding context to pin down a single location, the same rule Claude
Code's own edit tool follows.

**Read-only commands that skip confirmation:** `ls`, `pwd`, `whoami`, `date`,
`uname`, `df`, `du`, `ps`, `wc`, `which`, `cat`, `head`, `tail`, `echo`, `id`,
`hostname`, `free`, `uptime`. This only applies when the command contains **no
shell metacharacters** (`; | & < > ` $ ( )` or a newline) — so `ls; rm -rf ~`
and `echo hi > file` always ask. The same guard applies to `git`, so
`git log ; rm -rf ~` asks too.

**User-configurable permissions (C3)** — `~/.raven/settings.json`'s
`permissions` section extends this trust additively, never in place of it:

```json
{"permissions": {"allow_commands": ["pytest"], "allow_git": ["commit", "add"]}}
```

`allow_commands` lets a plain (still metacharacter-free) call to that
program — via `run_command` or `run_tests` — skip confirmation, on top of
the built-in read-only list above. `allow_git` does the same for git
subcommands, on top of `status`/`log`/`diff`/`show`. The shell-metacharacter
check always still applies regardless of this list: `"pytest; rm -rf ~"`
still confirms even with `pytest` trusted — trusting a program name only
ever exempts a plain call to exactly that program.

**Hooks (D1)** — the same `settings.json` can run a shell command
automatically before and/or after a specific tool call:

```json
{"hooks": {"post": {"edit_file": "black {path}"}, "pre": {"edit_file": "cp {path} {path}.bak"}}}
```

`{placeholder}`s are filled from that tool call's own arguments. Never
confirmed — being listed in this local, user-controlled file is the
approval, same trust boundary as `permissions`. A `post` hook is skipped if
the tool call's own result started with `"Error:"` (nothing actually
changed, so nothing to act on); a hook's output or error is appended to the
tool's own result rather than replacing or hiding it. Scoped to the model's
own tool-calling loop only — `/write`, `/run`, and the other manual slash
commands bypass this entirely, same as they already bypass some of the
Assistant-level confirmation logic.

### Applications and windows

| Tool | What it does | Confirms? |
|---|---|---|
| `list_apps` | List every installed app's display name | no |
| `open_app` | Launch an installed app by name | no |
| `open_file` | Open a file with the system default app, or with a named app | no |
| `close_app` | Close a running app — **every** window of it (process signal) | yes |
| `list_windows` | List open windows (id, app, title) | no |
| `close_window` | Close exactly **one** window, matched by title | yes |

How app handling works:

- **Discovery** reads `.desktop` files from native packages, Flatpak (system
  and per-user) and Snap, so apps from any of those are found.
- **Matching is fuzzy but strict.** Whole-word, both directions ("the text
  editor app" matches "Text Editor"). An ambiguous name returns the candidates
  instead of guessing; an unknown name returns the closest installed names.
- **VS Code opens files and folders in the window you already have** (`-r`),
  rather than spawning a new one each time. Other apps use their normal launch.
- **`close_window` is the only per-window control.** It needs the
  [Window Calls](https://github.com/ickyicky/window-calls) GNOME Shell
  extension; without it the tool says so instead of failing.

### Web

| Tool | What it does | Confirms? |
|---|---|---|
| `open_url` | Open a URL in the default browser, or a named one (e.g. Firefox) | no |
| `fetch_url` | Fetch a page and return its text (HTML stripped, ~4000 chars, 10 s timeout) | no |
| `web_search` | Search DuckDuckGo, return the top 5 titles, URLs and snippets | no |

### Browser interaction

A separate, persistent, **headless and isolated** Chromium (Playwright). It has
none of your real browser's cookies or logins, so it can read and interact with
public pages but can't act as you on sites you're signed into. It is started on
first use and stays alive across calls, so navigate → read → click → type →
submit acts on one continuous page. Elements are targeted by **visible text or
label**, not CSS.

| Tool | What it does | Confirms? |
|---|---|---|
| `browser_navigate` | Load a URL (JavaScript-rendered pages work, unlike `fetch_url`) | no |
| `browser_read` | Return the page's visible text (~4000 chars) | no |
| `browser_click` | Click a button or link | no — but it **refuses a real form-submit control** and redirects to `browser_submit` |
| `browser_type` | Type into a field by its label or placeholder | no |
| `browser_submit` | Click a control that commits an action (submit, buy, send, log in) | yes |
| `browser_close` | Close the browser session | no |
| `browser_screenshot` | Save a PNG of the current page to a temp file, return its path — pair with `analyze_image` to actually see it | no |

### Vision

| Tool | What it does | Confirms? |
|---|---|---|
| `analyze_image` | Look at an image file (screenshot, photo, diagram) and answer a question about it | no |

Uses a **separate**, configurable model (`settings.json`'s `model.vision`),
not the main chat model — the default may not support images at all. A
full-desktop screenshot (rather than just R.A.V.E.N's own browser page) isn't
supported yet; it needs the Wayland/GNOME screenshot portal's more involved
async permission flow.

### Gmail

Real inbox access through the Gmail API (OAuth). Granted scopes are exactly
`gmail.readonly` and `gmail.send`, so Google itself refuses to delete or alter
existing mail.

| Tool | What it does | Confirms? |
|---|---|---|
| `gmail_list_messages` | List recent messages (id, date, sender, subject); accepts Gmail search syntax such as `is:unread` or `from:x@y.com` | no |
| `gmail_read_message` | Read one message's sender, date, subject and body (~4000 chars) | no |
| `gmail_send_message` | Send a **new** email | yes — prompt shows the full To, Subject and Body |
| `gmail_reply_message` | Reply to an existing email, in its thread, to its `Reply-To` (else `From`); plain reply, not reply-all | yes — prompt shows the **resolved** recipient, Subject and Body |

For a reply, the prompt shows where the mail will really go, not just the
visible sender, because a sender can set `Reply-To` elsewhere. If that lookup
fails, the prompt still appears. A recipient containing a line break is refused.

---

## Voice input (V1)

`Ctrl+V` toggles push-to-talk: press to start recording, press again to stop.
Transcribed locally and offline via [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
— no network call, no OpenRouter quota used — and inserted into the input
buffer for review/editing, never auto-sent.

Needs two optional Python packages (`sounddevice`, `faster-whisper`) plus,
**on Linux only**, the system PortAudio library:

```bash
pip install sounddevice faster-whisper
sudo apt install libportaudio2   # Debian/Ubuntu; dnf/pacman equivalents on other distros
```

This is a real, unavoidable requirement for ANY Linux user who wants voice
input — `sounddevice`'s Linux wheel doesn't bundle PortAudio the way its
Windows/macOS wheels do, so without the system library it fails at import
with `OSError: PortAudio library not found`. None of it is required to run
R.A.V.E.N at all: missing dependencies just make `Ctrl+V` insert a
"voice input not set up" note instead of recording — everything else is
completely unaffected. The model weights (~140MB) are downloaded once via
the explicit `python -m raven.voice` command (use `HF_HUB_DISABLE_XET=1` if it
stalls at 0 bytes) — a keypress never downloads anything; until then `Ctrl+V`
shows "voice model not downloaded" in the toolbar. Transcription runs on a
background thread so the UI never blocks. `Ctrl+C` at the prompt clears the
line; `Ctrl+D` on an empty prompt exits. `Ctrl+V` is voice, not paste — use
the terminal's paste shortcut (usually `Ctrl+Shift+V`).

### Voice mode in the desktop app (Step 3.62)

A second, independent voice input path (the CLI's `Ctrl+V` is the first):
the desktop app's "Voice" mode captures audio in the browser (Web Audio API,
raw PCM, no compressed format/ffmpeg) and sends it to `raven/server.py`'s
new `transcribe` websocket message, which decodes it and calls the SAME
offline faster-whisper pipeline (`raven/voice.py`) the CLI already uses --
one transcription engine, two front ends. Requires the same setup as
`Ctrl+V` (`pip install ".[voice]"`, the model download).

Hold **Shift** to record, release to stop. Two behaviors, a checkbox toggle
(persisted in `localStorage`):
- **Off (default):** the transcript is shown, editable, with Send/Discard --
  never auto-sent, matching every other voice-input path in this project.
- **On ("Send automatically on release"):** the transcript is sent straight
  to the model as soon as it's back, no review step.

Disabled while disconnected or while a request is already running. A very
fast tap (Shift released before the microphone finishes opening) is handled
without getting stuck. Server errors (voice deps missing, model not
downloaded, transcription failure) surface inline in the Voice stage, not as
the shared chat error banner.

---

## Installing and the desktop app (Step 3.58)

`pip install .` (optional extra: `.[voice]`) installs the `raven` and `raven-server`
commands on any OS. The desktop app (`raven_ui/`) starts the Python server itself and
`npm run make` builds an installer for the current OS (Windows Setup.exe / macOS zip /
Linux deb+rpm). Only the Linux build has been exercised; installers are unsigned and the
app still requires Python with the package installed. Details: README "Setup" and "Desktop app".

### Cancelling a request (Step 3.69)

The Composer's Send button becomes a **Cancel** button while a request is in flight
(`raven/server.py`'s `cancel_ask` message). The underlying network call runs on a real
background thread and can't be forcibly stopped, so cancelling doesn't abort it --
instead the server marks that specific call's `_AskToken` cancelled, acknowledges
immediately (`cancel_requested`; UI shows "Cancelling..."), and once the call genuinely
finishes, rolls the turn back out of `Assistant.history` (so it's never sent to the
model or shown) and reports `cancelled`. Mirrors the CLI's own `Ctrl+C` semantics
("the half-finished turn is rolled back") as closely as a background-thread design
allows. A second `ask` is refused with a clear error until the current one -- cancelled
or not -- actually settles, preventing two turns from mutating history at once.
**Known limitation:** `Assistant.ask()` persists the turn to the SQLite store before
`_run_ask` ever learns it was cancelled, so the discarded turn is rolled back from the
live, in-memory conversation (correct for the rest of this session) but still exists on
disk -- a full app restart afterward would load it back. Not fixed; would need a new
`Store` method to delete specific rows after the fact.

### Message actions: copy, edit, resend (Step 3.72)

`MessageBubble.tsx`: every bubble gets a hover-revealed action row (`hidden group-hover:flex`,
same reveal pattern the sidebar's per-chat delete button already uses, so it never reserves
layout space while not hovered). **Copy** (the raw source text, not the rendered markdown) is
available on both roles; it briefly swaps its own label to "Copied" (a local `copied` state,
reset after 1.5s) instead of a toast. User messages only also get **Edit** and **Resend**, both
disabled while `busy` (mirrors Composer's own Send-button guard, since either would otherwise
race an in-flight turn -- `raven/server.py`'s `_AskToken` single-flight check would just refuse
it with an error, but disabling the buttons avoids that round trip entirely).
- **Copy's actual implementation (Step 3.75)**: NOT the plain Web `navigator.clipboard` API --
  Electron denies the `clipboard-write` permission to web content by default, and even a
  preload-based `require('electron').clipboard` call turned out not to be reliably available
  from inside preload's own copy of the `electron` module. `copy()` calls
  `window.electronClipboard.writeText(text)` (async, awaited -- see `src/electron.d.ts` for the
  exposed shape), which `preload.ts` relays via `ipcRenderer.invoke('clipboard:writeText', ...)`
  to a handler in `main.ts` that calls the real `clipboard` module from the (never sandboxed)
  main process. `setPermissionRequestHandler` in `main.ts` also now approves
  `clipboard-read`/`clipboard-sanitized-write` (alongside the existing `media` for voice), so
  the plain Web API works too if anything ever calls it directly.
- **Resend** calls the SAME `onSend`/`ask` callback the Composer's own Send button uses, with
  the message's exact text -- appends a new user message and lets the model answer again. It
  does NOT truncate or replace history: the original message and its original reply both stay,
  and the resend becomes new messages at the end, same as if you'd typed it again yourself.
- **Edit** loads the text into the Composer via a new `prefill` prop (`{text, token}` --
  the incrementing `token` forces the effect to re-fire even if the same message is edited
  twice in a row, since a plain string prop wouldn't change) instead of sending anything;
  the user reviews/changes it and sends normally. `draft` state lives in `ChatStage` itself,
  local to that stage -- neither `useRaven.ts` nor `App.tsx` needed to change.
- No backend changes at all -- this is a purely client-side feature built entirely from
  primitives the UI already had (the same `ask` callback, `useRaven`'s existing history).
- verified live in a real headless-Chromium run (a scripted fake `WebSocket`, no real server):
  Resend sends exactly `{"type":"ask","text":"<original>"}`; Edit loads the exact text into the
  textarea; both Edit and Resend correctly disable themselves once an `ask` is in flight; the
  assistant's own bubble has Copy but never Edit/Resend. Copy itself is verified separately,
  live, against the REAL Electron app (Step 3.75) -- the fake-`WebSocket` browser run above
  can't exercise `window.electronClipboard` at all, since that only exists inside a real
  Electron preload context.

### Notifications (Step 3.71)

A native OS notification fires from two places in `Assistant` itself (`raven/assistant.py`),
so the CLI and the desktop server both get it for free from one hook instead of two:
`_run_tool` notifies ("R.A.V.E.N needs confirmation", the exact confirmation prompt) right
before calling `confirm_run`, and `ask()` notifies ("R.A.V.E.N", the reply text -- collapsed
to one line, truncated at 200 chars) once a turn produces a non-empty reply. There's no
separate "needs advice" signal -- a reply that's actually a question back to the user still
arrives as a normal `ask()` reply, so it's covered by the same notification; the body text is
what tells the two apart. A tool that never needs confirmation never notifies; an empty reply
never notifies. Delivered via `raven/platforms`' existing OS-abstraction (`Backend.notify`),
same pattern as the app/window-control tools: `notify-send` on Linux, `osascript` on macOS, a
PowerShell WinRT toast on Windows. Unlike every other `Backend` method, `notify` never raises
and returns nothing -- each OS implementation swallows its own failure (missing binary,
subprocess error, timeout) internally, since a missed notification must never break the
confirmation/reply it was announcing. macOS/Windows are unconfirmed on real hardware, same
honest posture as the rest of `platforms/` (Step 3.57).

### Design mode: a mathematical drawing playground (Step 3.76)

A new `"design"` skill (`raven/tools.py`) and a Figma-like SVG canvas in the desktop app
(`raven_ui/src/stages/DesignStage.tsx`, wired into `App.tsx`'s previously-empty `mode ===
'design'` branch). Every shape is generated server-side by a pure `params -> geometry`
function in the new `raven/design.py`:

| Tool | Generator | Produces |
|---|---|---|
| `draw_golden_rectangle` | `golden_rectangle` | a rectangle in golden-ratio proportions |
| `draw_golden_spiral` | `golden_spiral` | a logarithmic spiral (golden-ratio growth) |
| `draw_polygon` | `regular_polygon` | an N-sided regular polygon |
| `draw_star` | `star_polygon` | an N-pointed star |
| `draw_radial_pattern` | `radial_pattern` | a motif repeated evenly around a center (mandalas) |
| `draw_grid` | `grid_layout` | an empty column/row grid of placeholder cells |
| `draw_bezier` | `bezier_curve` | a 2/3/4-control-point line/quadratic/cubic curve |
| `draw_lissajous` | `lissajous_curve` | a closed parametric decorative loop |

Plus `list_canvas_elements`/`update_canvas_element`/`delete_canvas_element` to read/edit/
remove what's already drawn. All 11 are intercepted in `Assistant._run_tool` (same pattern
as `load_skill`/`analyze_image`) since they need live `self.canvas` access; **none require
confirmation** -- they only ever touch this session's own saved canvas, never anything
external. Every element's rendered geometry (points/path data) is always *derived* from its
own numeric `params` (sides, radius, turns, control points, ...) by re-running the same
generator -- editing a shape later (by R.A.V.E.N or by hand in the UI) regenerates it exactly,
never approximates it.

The canvas persists per chat session (a new `canvas_elements` table in `raven/store.py`,
alongside `messages`) and is pushed to the UI live as R.A.V.E.N draws (a `canvas_update`
websocket message per element, not just once the whole reply arrives). The UI supports
click/shift-click multi-select, drag-to-move, resize handles (regenerating a shape's own
"primary size" param where one exists, e.g. a polygon's `radius`), a rotate handle, a layers
panel (z-order + delete), a properties panel (numeric params + fill/stroke color), group/
ungroup (a group carries its own position/rotation/scale on top of each child's still-
independent params -- ungrouping bakes that back onto each child exactly), pan (space+drag)
and zoom (wheel, around the cursor), and a client-side undo/redo stack that replays the same
edit/delete/restore calls a live action would make, so the server stays authoritative
throughout. Hand-edits you make in the UI are summarized into R.A.V.E.N's context
automatically before her next reply (`[Canvas changed since your last turn: ...]`), so asking
her to act on something you just moved or recolored doesn't rely on stale information.
Built entirely in plain SVG + pointer events, no new frontend dependency. The canvas itself
is visually delimited: the SVG viewport has a plain border, and a second, non-interactive
rect marks the canvas's actual fixed extent regardless of pan/zoom, so it's always clear
where the drawing surface is even after scrolling away from it.

Numeric tool arguments are defensively coerced (`Assistant._coerce_numeric_strings`, Step
3.77) before reaching any generator -- some models send them as JSON strings (`"80"` instead
of `80`) despite the tool spec declaring a numeric type, which crashed every draw call before
this existed (a real, live-confirmed failure mode, not a hypothetical one).

---

## Skills (D2)

Only "core" (file I/O, search, shell, coding, `save_note` — 13 tools) plus
`load_skill` itself are sent to the model on every request; `desktop`, `web`,
`browser`, and `gmail` (19 tools total) are grouped skills, loaded on demand.
This exists to fix a measured, fixed cost: every registered tool's full spec
(name, description, parameter schema) is normally sent on **every** request
regardless of what that conversation is about — measured at ~2.9k tokens for
26 tools back when this was first flagged (DEV_LOG, 2026-09-21), now ~4k for
32. Splitting that into core + on-demand skills, and moving each skill's
guardrail prose (the Gmail Reply-To/untrusted-content rules, the browser
submit-safety rule, the app-name-matching guidance) out of the always-sent
system prompt into text appended only once that skill is actually loaded,
measured at roughly **halving** the fixed overhead (~5.6k → ~2.75k tokens)
for a conversation that only ever touches core tools.

How it works: `load_skill(name)` is a special tool, handled by `Assistant`
itself rather than `tools.py` — it doesn't touch the outside world, it
changes what's offered to the model next. Calling it adds that skill's tool
names to the next request's tool list, and (for a real provider) appends its
guardrail text to the live `system_prompt` in place, so nothing else needs to
change to pick it up on the following request. Never asks for confirmation
(it's not consequential — it only expands what's available, exactly the way
core tools already were), and is idempotent (loading an already-loaded skill
just says so). Nothing about a skill's tools changes once loaded — same
confirmation policy as always; this only affects **when** their specs and
instructions reach the model, not how they behave. A skill's activation is
per-session, in-memory only, same reasoning as B2/B3's decision not to
persist compaction summaries or notes-loading state — a fresh session always
starts back at core-only, and re-loading a skill if/when it's needed again is
cheap and correct either way.

---

## Confirmation model

Eleven tools can ask for permission. What each guard actually is:

| Tool | Asks when | Enforced by |
|---|---|---|
| `write_file` | only if the target already exists (overwriting) | code |
| `edit_file` | always (only ever touches an existing file) | code |
| `delete_file` | always | code |
| `close_app` | always ("close ALL windows of X") | code |
| `close_window` | always | code |
| `run_command` | unless a read-only program or a pipeline of read-only programs (e.g. `pdftotext f.pdf - \| head`), or user-trusted (`permissions.allow_commands`) | code (allow-list plus metacharacter check) |
| `run_tests` | same policy as `run_command` | code |
| `git` | unless `status`/`log`/`diff`/`show`, or user-trusted (`permissions.allow_git`), with no metacharacters | code |
| `browser_submit` | always | code |
| `gmail_send_message` | always | code |
| `gmail_reply_message` | always | code |

A denied request returns "Cancelled by user", and the model is instructed to
say plainly that nothing was done. The model is also instructed **not** to ask
in prose first, since the prompt already exists.

**Where the boundary is model-dependent, not code-enforced:**

- `browser_click` only catches *literal* HTML submit controls. A JavaScript
  "Buy Now" that commits without being a `<button type=submit>` would not be
  caught by it; the model is told to use `browser_submit` for anything that
  commits.
- Choosing `close_window` over `close_app`, `gmail_reply_message` over
  `gmail_send_message`, or `open_url` with an app name over the default browser
  is the model's call, guided by tool descriptions.
- **Prompt-injection defence is instruction-only.** The model is told that web
  page content and email content are data, never instructions. The
  confirmation prompts above are the hard backstop for anything consequential.

### `sudo` inside `run_command`/`run_tests` (Step 3.73)

The confirmation policy above is unchanged by this -- a `sudo` command still
needs the same `Allow?` approval as any other `run_command` call. What
changed is what happens AFTER approval: `run_command`/`run_tests` normally run
with `stdin=subprocess.DEVNULL` (deliberate, see the comment at `tools._run_shell`
-- without it, an interactive command inherits R.A.V.E.N's real terminal and
can hang unrecoverably), which meant `sudo` could never actually read a
password, so an approved `sudo` call just failed afterward with no way to
supply what it needed.

`tools._run_sudo` (only entered when the command's first word is literally
`sudo`, via `_SUDO_RE`) tries `sudo -n <rest>` first -- succeeds immediately,
asks for nothing, if `sudo`'s own cached credential (~15 min ticket) is still
valid. Only if that specifically fails with "a password is required" does it
consult `tools._sudo_password_provider` (a module-level hook, `None` by
default) for a password, then retries as `sudo -S -p '' <rest>` with the
password piped through the real subprocess's stdin -- never spliced into the
command string (would leak into a process listing and this tool's own
returned output) and never logged. `raven/cli.py`'s interactive `main()` is
the ONLY thing that ever registers a provider (`sudo_password_prompt`, a thin
`getpass.getpass()` wrapper with the same Status-spinner pause/resume dance
`confirm_run` already does) -- `run_headless` (`-p`/`-y`) and `raven/server.py`
(the desktop app) never do, since neither has a terminal it's safe to
interactively prompt on; a `sudo` call there fails exactly as it always has
(a clean "a password is required" in the tool's own output, never a hang).
Declining the password prompt (Enter/Ctrl+C/Ctrl+D) returns "Cancelled: no
sudo password entered." without ever running the real command.

Both the probe and the real authenticated call run through `tools._run_capturing_group`
(`subprocess.Popen(..., start_new_session=True)` + `.communicate()`) rather than
plain `subprocess.run`, specifically so a timeout kills the WHOLE process group,
not just the `/bin/sh -c "..."` wrapper -- see Step 3.74: a real privileged
command (an install, an upgrade) can run past a short timeout, and an orphaned
`sudo` left attached to R.A.V.E.N's own controlling terminal can fall back to
prompting there directly, outside any of this code's control. The real,
authenticated call gets its own, much longer `RUN_SUDO_TIMEOUT` (300s) for the
same reason `run_tests` gets a longer cap than `run_command` -- the `-n` probe
keeps whatever timeout its caller (`run_command`/`run_tests`) was given, since
it always resolves near-instantly either way.
- verified: 6 new `tools.py` tests (cached-credential case never asks;
  no-cached-credential case asks and the password reaches the real process
  only via `input=`, never the command string or `stdin=`; declining runs
  nothing; no registered provider fails exactly like before; a command that
  merely mentions "sudo" without starting with it is untouched), 6 new
  `cli.py` tests for `sudo_password_prompt` (returns what `getpass` returns;
  empty/Ctrl+C/Ctrl+D all mean declined; the Status spinner is paused and
  resumed around the prompt, including when declined; `run_headless` never
  registers a provider). Live-verified on this real machine: the real `sudo`
  binary's actual stderr text ("sudo: a password is required") matches what
  the detection checks for exactly; the real `sudo -S -p '' true` mechanics
  (password via `input=`) confirmed end-to-end with a deliberately WRONG
  password (never a real one) -- "Sorry, try again." / "1 incorrect password
  attempt" proves the piped password genuinely reached sudo's own auth check,
  not just that the command ran. Step 3.74 additionally proved, live, that
  the OLD plain-`subprocess.run` version left a real orphaned process behind
  on a timeout (`pgrep` found it, PID confirmed) while the current
  `_run_capturing_group`-based version does not, for the same shape of
  command. Full suite: 545 passed.
- not done: a command where `sudo` isn't the very first word (a pipeline like
  `foo | sudo tee /etc/x`, or `sudo` after `&&`) isn't detected -- stated
  scope limit, same "only the plain, common case" posture `_is_safe_command`'s
  own SAFE_COMMANDS matching already has elsewhere in this file. A wrong
  password can show a slightly noisy combined message (`sudo -S`'s own retry
  reads EOF on its second attempt, since the pipe only ever supplies one line)
  -- still clearly reports failure, just not the cleanest possible wording;
  not fixed since it doesn't affect correctness. The general, non-sudo
  `run_command`/`run_tests` path still uses plain `subprocess.run` (only
  wrapper-level timeout killing) -- the same theoretical orphan risk exists
  there for a long-running compound command, but nothing else in that path
  can hijack the real terminal the way an orphaned `sudo` can, so it wasn't
  changed.

---

## Memory and context

- **Persistent history** in `~/.raven/history.db` (SQLite), loaded on startup.
  Only completed turns are saved; a cancelled or errored turn is not.
- **Bounded context.** Only as much recent history as fits an ~12k-token
  budget (a chars/4 approximation) is sent to the model, trimmed so the window
  never starts on an orphaned tool result. This is a practicality budget for a
  slow free tier, not a model context-window limit. Full history is kept in
  memory and on disk regardless.
- **Auto-compaction.** Whatever falls out of that budget isn't just dropped —
  it's summarized into a single running summary, sent in place of the raw
  turns it replaces. Compaction runs automatically whenever the budget
  boundary advances (cached and only extended incrementally, so a stable
  conversation doesn't re-summarize every turn), or on demand with
  `/compact`. It's a dedicated LLM call, capped at ~2000 summary characters
  as a backstop. Crucially, it never touches the real, persisted history —
  `self.history` and `~/.raven/history.db` stay the full raw record; only
  what's *sent per request* is affected, so a fresh session always starts
  from the untouched original and nothing is ever actually lost, only
  compressed for the model's sake.
- **Bounded tool output.** Genuinely unbounded tool results (`read_file`,
  `run_command`, `list_dir`) are capped at ~4000 characters, with a "truncated,
  N more chars" marker — the same convention `fetch_url`, `browser_read`, and
  Gmail message bodies already used.
- **`/forget`** wipes it. History can go stale (an app you installed after the
  model said it wasn't there), so the model is told to re-run the relevant tool
  instead of trusting an earlier conclusion, but `/forget` is the reset.
- **Cross-session notes** (`save_note`, `~/.raven/memory/notes.md`) are a
  separate, durable memory the model writes to on its own initiative — a
  preference, a convention, a correction — outside conversation history
  entirely, so they survive `/forget` and are loaded into every future
  session's system prompt. Bounded on the write path rather than trusted to
  the model's judgment: one note capped at ~500 characters, the file at
  ~4000; both refuse outright rather than truncate a fact or evict an older
  note once full. Never needs confirmation (append-only, bounded, and the
  file is just plain markdown the user can read or edit by hand — see
  `/notes`).
- **Tool loop cap:** at most 10 tool-call rounds per request. Past that it
  stops and says so, rather than looping.
- **No emojis.** Stripped from replies and reasoning in code, since free
  models routinely ignore the instruction not to use them.

---

## Project/user rules

An optional `RAVEN.md` in the current working directory, and/or an optional
`~/.raven/RAVEN.md`, are loaded into the system prompt once at startup —
project-specific conventions (coding standards, how to run tests, anything
the model should know about that project) or personal preferences that apply
everywhere. Verified live: a rule in a project's `RAVEN.md` ("always end
every reply with X") was genuinely followed by the model, not just appended
to the prompt text.

- If both are present, user rules come first and project rules last, so the
  more specific one wins on anything they disagree on.
- Each file is capped at ~4000 characters (same convention as tool output).
- An empty, missing, or unreadable file is silently treated as absent —
  never an error.
- R.A.V.E.N reports on startup which files, if any, were loaded.

---

## Model and configuration

- **Provider (Step 3.63):** `model.provider` in `~/.raven/settings.json` picks
  `OpenRouterProvider` or `NvidiaProvider` (`raven/llm_provider.py`); default
  is `"openrouter"`. Each reads its own API key from its own env var, so
  switching one never touches the other's.
- **No .env required (Step 3.80, release prep):** `config.get_api_key(provider,
  settings)` checks `settings.json`'s `api_keys` section FIRST, falling back
  to the env var only if that's empty — a key saved via the CLI's first-run
  onboarding / `/apikey`, or the desktop app's Settings panel, always wins.
  The CLI runs onboarding automatically on first launch (no name set, or no
  key for the configured provider): asks for the user's name once, then a
  provider + key if none is configured, and saves both. `/name [name]`,
  `/provider [name]`, `/apikey [provider]` change any of it later without a
  restart — each rebuilds the live `Assistant.provider` in place when it's
  the active provider, same posture as the desktop app's `set_model`. The
  desktop app's Settings panel gets the same two fields (an API key input per
  provider, and a "Your name" field) via new `get_user_config`/`set_api_key`/
  `set_user_name` server messages — `has_key` booleans only, the key itself is
  never echoed back once saved.
  - **OpenRouter:** one request lists all candidate models
    (`model.name` + `model.fallbacks`); OpenRouter tries them in order
    server-side. Defaults: `nvidia/nemotron-3.5-lightning:free`, falling back
    to `google/gemma-4-31b-it:free`, then `openrouter/free`. Key:
    `OPENROUTER_API_KEY`.
  - **NVIDIA (build.nvidia.com's free NIM API):** an ordinary OpenAI-compatible
    endpoint with a single `model` field, so fallbacks are tried client-side —
    one HTTP request per candidate, stopping at the first success. Key:
    `NVIDIA_API_KEY`. `list_nvidia_models()` lists the models a key can use
    (`GET /v1/models`), which powers the desktop app's model picker
    (Settings → Model); OpenRouter has no such listing wired up yet, so its
    model is still typed in directly.
  - The desktop app can switch provider/model live (Settings panel → the
    server's `set_model`/`list_models`/`get_model_config` messages): the new
    provider is constructed and validated *before* anything is persisted or
    swapped in, so a bad choice (missing key, unknown provider) leaves the
    current, working model untouched. Switching provider clears
    `model.fallbacks` — the old list is model ids for the other provider.
- **Requests time out after 60 s** and the provider's own error text is
  surfaced, not hidden behind a generic HTTP error.
- **Connection reuse (Step 3.67):** each provider keeps one persistent
  `requests.Session()` for its whole lifetime instead of opening a fresh
  connection per call — measured live at ~60-70% less latency per call after
  the first, which compounds across a turn's several sequential tool-call
  round trips (and `NvidiaProvider`'s own back-to-back fallback calls).
- **Token accounting** comes from the API's `usage` figures.
- **Settings** (all optional) live in `~/.raven/settings.json`: `model.provider`,
  `model.name`, `model.fallbacks`, `statusline.segments`, `api_keys` (per
  provider), `user.name`.
- **Swappable provider:** implement `LLMProvider.reply()` in
  `raven/llm_provider.py`; `cli.py`/`server.py` each branch on
  `model.provider` to pick the class.
- **Persona:** professional, calm and concise. The system prompt also carries
  what the name stands for (Reasoning Agent for Versatile Execution &
  Negotiation).
- **Project/user rules:** an optional `RAVEN.md` in the working directory
  and/or `~/.raven/RAVEN.md` are loaded into the system prompt at startup —
  user rules first, project rules last (closer to the task, so they win on
  conflict). Each capped at ~4000 characters. Reported on startup when found.

### Files R.A.V.E.N reads or writes

| Path | Purpose |
|---|---|
| `.env` (project) | Optional fallback for `OPENROUTER_API_KEY`/`NVIDIA_API_KEY`/`FREELLMAPI_API_KEY` -- `~/.raven/settings.json`'s `api_keys` (set via onboarding, `/apikey`, or the desktop Settings panel) takes precedence |
| `RAVEN.md` (project) | Optional project-specific rules, loaded into the system prompt |
| `~/.raven/RAVEN.md` | Optional user-wide rules, loaded into every project's system prompt |
| `~/.raven/history.db` | Conversation history |
| `~/.raven/settings.json` | Model and status-line settings (optional) |
| `~/.raven/gmail_credentials.json` | Your Google OAuth client secret (you provide it) |
| `~/.raven/gmail_token.json` | Cached Gmail login, created on first consent |

---

## Prerequisites

Not everything works out of the box; some tools depend on your system.

| Needed for | Requires |
|---|---|
| Everything | Python 3.10+, an OpenRouter API key |
| App launching and file opening | `gtk-launch`, `xdg-open` (Linux desktop) |
| `close_app` | `pkill` |
| `list_windows` / `close_window` | `gdbus` and the Window Calls GNOME Shell extension (GNOME) |
| Browser tools | `playwright` and a one-time `playwright install chromium` (~115 MB) |
| Gmail tools | A Google Cloud project with the Gmail API, an OAuth Desktop client, and your account added as a test user — a one-time setup only you can do (see README, "Gmail access") |

---

## Known limitations

**Missing safeguards**
- `/run` always asks, even for commands the agent itself runs without asking
  (`ls` and the like). The two paths use different rules.

**Platform**
- **Linux is the only platform where app/window control has been run for real.**
  Linux uses `.desktop` files, `gtk-launch`, `xdg-open` and (for windows) the
  **GNOME + Wayland + Window Calls extension**. Windows and macOS backends exist
  (`raven/platforms/windows.py`, `macos.py`; Step 3.57) and are unit-tested with faked
  OS calls, but have **not been run on real Windows/macOS** -- see the two test checklists.
  Windows: apps come from Start Menu shortcuts (Store/UWP apps without one aren't
  listed), windows are each process's *main* window only, closing is polite
  (unsaved-changes prompts can refuse). macOS: apps are `.app` bundles, closing
  uses AppleScript (checks the app is running first so it never launches one just
  to quit it), and listing/closing windows needs Accessibility permission for the
  terminal app. The read-only command allow-list is Windows-aware (`dir`, `type`,
  `where`, `findstr`; `^` and `%VAR%` treated as unsafe).
- `close_app` closes **all** windows of an app. Per-window closing is only
  `close_window`.

**Web and browser**
- The isolated browser **cannot see your real browser** — not your open tabs,
  not your logged-in sessions. `open_url` can open a page in your real Brave or
  Firefox, but only as fire-and-forget; nothing can be read back from it.
- `web_search` uses DuckDuckGo's free, keyless endpoint, which rate-limits after
  a burst of searches; it then returns a "temporarily rate-limited" message.

**Gmail**
- No reply-all, forward, drafts, attachments, or HTML composing (plain-text
  bodies only). Cannot delete or modify existing mail.
- Reading prefers plain text, falling back to HTML with tags stripped;
  attachments are ignored.

**Model**
- Runs on free OpenRouter models: latency varies, the model that answers can
  change from call to call, and there is a daily request cap.

**Not built yet**
- Voice *output* (text-to-speech) — voice *input* is built (see "Voice input" above).
- A full-desktop screenshot for vision — only R.A.V.E.N's own browser page
  can be captured (`browser_screenshot`) until the Wayland/GNOME screenshot
  portal's async flow is built.
- `analyze_image`'s live model call is unverified as of 2026-09-23 — built
  and unit-tested, but never yet confirmed against a real vision model
  (blocked by OpenRouter's daily quota that day); check DEV_LOG before
  assuming it works end to end.
- Streaming replies (text appearing as it is generated).
- Searching or recalling *past* sessions — history is one continuous
  conversation, not searchable.
- Task-specific modes (coding / research / planning) — Stage 4 on the roadmap.
- An HTTP/API wrapper for other front-ends — Stage 5.

---

## Verification notes

Nearly everything above was exercised live during development (see
`DEV_LOG.md`, which records each check). The exceptions worth knowing:

- **Sending mail has never been run for real.** `gmail_send_message` and
  `gmail_reply_message` were verified up to the API's send call — message
  construction, threading headers, recipient resolution, and the confirmation
  preview against a fake Gmail service, plus the read and metadata calls against
  the real API — but no real email has been sent, since that would message a real
  person as you. Try one to yourself first.
- **Gmail body reading** (`gmail_read_message`) was verified against fabricated
  message payloads, not against real message bodies.
- **VS Code window reuse** was verified at the argument level (the exact command
  is `code -r <path>`); a real VS Code window was deliberately not launched to
  confirm the reuse.
- **`Ctrl+O` live toggle** was verified at the function level (same data,
  different output) but not observed repainting in a real terminal from
  the development side.

---

## Where things live

| File | Responsibility |
|---|---|
| `raven/cli.py` | Entry point, slash commands, key bindings, live activity line, rendering |
| `raven/assistant.py` | Conversation state, the tool-call loop, the confirmation policy, emoji stripping, context trimming |
| `raven/llm_provider.py` | The system prompt, `RAVEN.md` rules loading, the provider interface, the OpenRouter client |
| `raven/tools.py` | File, shell, search, app, window and web tools, plus the tool registry and specs |
| `raven/browser.py` | The Playwright browser session (runs on its own thread) |
| `raven/gmail.py` | Gmail OAuth, listing, reading, sending and replying |
| `raven/store.py` | SQLite history persistence |
| `raven/statusline.py` | Bottom status-line segments |
| `raven/config.py` | Environment and `settings.json` loading |
| `tests/` | The pytest suite — one file per module, plus `conftest.py` for shared fixtures |
