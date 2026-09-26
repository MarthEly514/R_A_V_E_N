# Windows smoke-test checklist

For a tester on a Windows machine. Status (2026-09-26): the portable core
(files, search, edit, git, web, browser, Gmail, voice, terminal + desktop UI)
and the **Windows app/window backend** (Step 3.57) are both written and unit-tested
with faked Windows calls, but **never run on real Windows**. This checklist is
that first real run.

## Setup
```powershell
git clone <repo> ; cd R_A_V_E_N
python -m venv venv ; venv\Scripts\activate
pip install -r requirements.txt fastapi "uvicorn[standard]" httpx numpy pytest
# .env with OPENROUTER_API_KEY=... (free key from openrouter.ai)
```

## 1. Automated tests
```powershell
python -m pytest -q --ignore=tests\test_browser.py
```
Report the summary line and paste any failure output. (Failures here are
expected in places — Unix-specific tests — and are exactly what we want to find.)

## 2. Start it
```powershell
python -m raven.cli
```
- [ ] Banner and prompt appear; you can type immediately.
- [ ] Bottom status line shows (model / context / tokens / memory).
- [ ] `/status` prints CPU, Memory, Disk without an error.
- [ ] `/help` works; `/exit` quits; Ctrl+C at the prompt clears the line (no traceback).

## 3. Ask it things (each should just work)
- [ ] "List the files in this folder" — no confirmation prompt.
- [ ] "Read README.md and summarize it" — no confirmation prompt.
- [ ] "Create a file hello.txt containing 'hi'" — created; asking to overwrite it later *does* prompt.
- [ ] "Run `dir`" — runs **without** asking (Windows read-only command).
- [ ] "Run `del hello.txt`" — **asks** for confirmation first.
- [ ] "Search the web for python 3.13 release date" — returns results.

## 4. App and window control (new, untested on real Windows)
Each should work; if not, copy the exact reply R.A.V.E.N gives.
- [ ] "List my installed apps" — a list of names from the Start Menu appears.
- [ ] "Open Notepad" (or Calculator) — it launches.
- [ ] "Open notes.txt in Notepad" (a real file) — Notepad opens that file.
- [ ] "Open hello.txt with the default app" — opens.
- [ ] "List my open windows" — shows an id, program and title for each open window.
- [ ] "Close the Calculator window" — asks for confirmation, then closes only that window.
- [ ] "Close Notepad" — asks for confirmation, then closes it (an unsaved file should make Notepad *ask to save*, not vanish).
- [ ] Store apps (e.g. Calculator on Windows 11) may **not** appear in the app list — known limitation, note whether it happens.

## 5. Optional
- [ ] Desktop app: `python -m raven.server`, then in `raven_ui`: `npm install` and `npm start`.
- [ ] Voice: `python -m raven.voice` (downloads the model), then Ctrl+V in the CLI.

## What to send back
OS version, Python version, the pytest summary, and for any failed box: the
exact command/prompt and the full error text.
