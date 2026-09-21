# R.A.V.E.N

CLI assistant. Currently uses Openrouter's API; swap in a local model later
by writing a new class in `raven/llm_provider.py` that implements
`LLMProvider.reply()` and using it in `cli.py`.

## Setup

```bash
pip install -r requirements.txt
export OPENROUTER_API_KEY=your-key-here
python -m raven.cli
```

## Structure

```
raven/
  llm_provider.py   # LLM interface + Provider (Openrouter) implementation
  assistant.py       # conversation state, calls the provider
  tools.py           # file I/O + shell primitives, tool specs
  store.py           # SQLite persistence for conversation history
  statusline.py      # bottom status-line segments
  config.py          # env/config loading + settings.json
  cli.py              # entry point, input/output loop
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

Status-line segments: `model` (what actually answered), `context` (messages sent /
cap), `tokens` (session total), `memory`, `cpu`. Add your own in
`raven/statusline.py` — one function per segment.

Change it from inside R.A.V.E.N with `/statusline` (no args lists the segments;
`/statusline model tokens cpu` sets and saves them).

`model.fallbacks` are tried in order when the primary free model is down or
rate-limited (OpenRouter's `models` routing).

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


