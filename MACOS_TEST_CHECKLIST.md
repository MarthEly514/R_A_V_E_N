# macOS smoke-test checklist

Status (2026-09-26): the macOS app/window backend (Step 3.57) is written and
unit-tested with faked `osascript`/`open`, but **never run on a real Mac**.

## Setup
```bash
git clone <repo> && cd R_A_V_E_N
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt fastapi "uvicorn[standard]" httpx numpy pytest
echo 'OPENROUTER_API_KEY=...' > .env
python -m pytest -q --ignore=tests/test_browser.py     # paste the summary line
python -m raven.cli
```

## Ask it
- [ ] "List my installed apps" — app names (Safari, Notes, …) appear.
- [ ] "Open Notes" — launches.
- [ ] "Open notes.txt in TextEdit" (a real file) — TextEdit opens it.
- [ ] "Open hello.txt with the default app" — opens.
- [ ] "List my open windows" — **first time, macOS asks/denies**: R.A.V.E.N should say to grant
      *Accessibility* to your terminal (System Settings → Privacy & Security → Accessibility). Grant it, retry, and windows should list.
- [ ] "Close the <one window's title> window" — asks confirmation, closes only that window.
- [ ] "Close Notes" (while running) — asks, then quits it. With Notes **not** running, it must say
      "does not appear to be running" and must **not** launch it.

## Send back
macOS version, chip (Intel/Apple silicon), the pytest summary, and the exact reply for anything that failed.
