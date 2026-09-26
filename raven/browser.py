"""Playwright-backed browser automation: a persistent, isolated Chromium
session (no cookies/logins carried over from your real browser) that
R.A.V.E.N can navigate and interact with. Lazily started on first use and
kept alive across calls, so a workflow (navigate -> click -> type -> submit)
sees one continuous page instead of a fresh browser each time.

Selectors are text-based (visible text / accessible label), not CSS — the
model can act on what it reads on the page without needing to see raw HTML.

All Playwright calls run on one dedicated background thread (see
_run_in_browser_thread), for two reasons: Playwright's sync API isn't
thread-safe across threads, so a persistent session needs to stay pinned to
a single thread anyway — and, confirmed by reproducing it directly, leaving
Playwright's sync API "started" on the main thread corrupts that thread's
asyncio state in a way that crashes prompt_toolkit's own asyncio.run() call
on the next prompt (RuntimeError: asyncio.run() cannot be called from a
running event loop). Confining Playwright to its own thread fixes both.
"""
import atexit
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

from playwright.sync_api import sync_playwright

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="raven-browser")

_playwright = None
_browser = None
_page = None

MAX_READ_CHARS = 4000

# True only for an actual form-submit control: a <button>/<input type=submit>,
# or a bare <button> inside a <form> (which defaults to type=submit per the
# HTML spec). Used to force submitting actions through browser_submit (which
# requires confirmation) instead of the unconfirmed browser_click.
_SUBMIT_CHECK_JS = """
(el) => {
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  if ((tag === 'button' || tag === 'input') && type === 'submit') return true;
  if (tag === 'button' && !el.hasAttribute('type') && el.closest('form')) return true;
  return false;
}
"""


def _run_in_browser_thread(fn, *args, **kwargs):
    """Run fn on the dedicated browser thread and block for the result."""
    return _executor.submit(fn, *args, **kwargs).result()


def _ensure_page():
    global _playwright, _browser, _page
    if _page is None:
        _playwright = sync_playwright().start()
        _browser = _playwright.chromium.launch(headless=True)
        _page = _browser.new_page()
    return _page


def _locate(page, text: str):
    """First visible element matching `text`, trying the most specific
    (and most action-relevant) match type first."""
    for locator in (
        page.get_by_role("button", name=text, exact=False),
        page.get_by_role("link", name=text, exact=False),
        page.get_by_label(text, exact=False),
        page.get_by_placeholder(text, exact=False),
        page.get_by_text(text, exact=False),
    ):
        try:
            if locator.count() > 0:
                return locator.first
        except Exception:
            continue
    return None


def _navigate(url: str) -> str:
    if not url.startswith(("http://", "https://", "file://")):
        url = f"https://{url}"
    page = _ensure_page()
    page.goto(url, wait_until="domcontentloaded", timeout=20000)
    return f'Loaded {page.url} — "{page.title()}"'


def _read() -> str:
    page = _ensure_page()
    if page.url == "about:blank":
        return "No page loaded yet — call browser_navigate first."
    text = " ".join(page.inner_text("body").split())
    if len(text) > MAX_READ_CHARS:
        text = text[:MAX_READ_CHARS] + " … (truncated)"
    return f'{page.url} — "{page.title()}"\n{text}'


def _screenshot() -> str:
    """Save a PNG of the current page to a throwaway temp file, return its
    path (V2, vision skill) -- a single synchronous Playwright call, unlike
    a real DESKTOP screenshot (deferred, see DEV_LOG Stage V: that needs the
    XDG portal's async request/signal dance, real added complexity this
    doesn't need since Playwright already owns the page). The caller is
    expected to follow up with analyze_image(path, question) -- this tool
    only captures, it doesn't interpret."""
    page = _ensure_page()
    if page.url == "about:blank":
        return "No page loaded yet — call browser_navigate first."
    fd, path = tempfile.mkstemp(suffix=".png", prefix="raven-screenshot-")
    os.close(fd)
    page.screenshot(path=path)
    return path


def _click(text: str) -> str:
    page = _ensure_page()
    el = _locate(page, text)
    if el is None:
        return f"No visible element matches '{text}'."
    if el.evaluate(_SUBMIT_CHECK_JS):
        return f"'{text}' submits a form — use browser_submit for it, not browser_click."
    el.click(timeout=5000)
    return f"Clicked '{text}'."


def _type_text(text: str, value: str) -> str:
    page = _ensure_page()
    for locator in (
        page.get_by_label(text, exact=False),
        page.get_by_placeholder(text, exact=False),
        page.get_by_role("textbox", name=text, exact=False),
    ):
        try:
            if locator.count() > 0:
                locator.first.fill(value, timeout=5000)
                return f"Typed into '{text}'."
        except Exception:
            continue
    return f"No input field matches '{text}'."


def _submit(text: str) -> str:
    page = _ensure_page()
    el = _locate(page, text)
    if el is None:
        return f"No visible element matches '{text}'."
    el.click(timeout=5000)
    return f"Submitted '{text}'."


def _close() -> str:
    global _playwright, _browser, _page
    if _browser:
        _browser.close()
        _playwright.stop()
    _playwright = _browser = _page = None
    return "Browser session closed."


def navigate(url: str) -> str:
    """Go to a URL in R.A.V.E.N's own isolated browser (no saved logins).
    No confirmation needed."""
    return _run_in_browser_thread(_navigate, url)


def read() -> str:
    """Return the current page's visible text (rendered, post-JavaScript —
    unlike fetch_url, this sees JS-built content). No confirmation needed."""
    return _run_in_browser_thread(_read)


def screenshot() -> str:
    """Save a PNG of the current page to a temp file and return its path
    (V2). No confirmation needed — capturing an image isn't consequential;
    follow up with analyze_image(path, question) to actually interpret it."""
    return _run_in_browser_thread(_screenshot)


def click(text: str) -> str:
    """Click a visible button or link matching `text`. No confirmation needed
    — UNLESS the target is a form-submit control, in which case this refuses
    and says to use browser_submit instead (that one requires confirmation)."""
    return _run_in_browser_thread(_click, text)


def type_text(text: str, value: str) -> str:
    """Type `value` into the input/field matching `text` (its label,
    placeholder, or accessible name). No confirmation needed."""
    return _run_in_browser_thread(_type_text, text, value)


def submit(text: str) -> str:
    """Click a submit/commit control (a button or link that finalizes an
    action — submitting a form, completing a purchase, sending something)
    matching `text`. Requires user confirmation."""
    return _run_in_browser_thread(_submit, text)


def close() -> str:
    """Close R.A.V.E.N's browser session. No confirmation needed — it's only
    closing R.A.V.E.N's own isolated browser, not anything of yours."""
    try:
        return _run_in_browser_thread(_close)
    except RuntimeError:
        # Only happens at interpreter shutdown, when this runs via atexit but
        # ThreadPoolExecutor's own exit handling has already torn the pool
        # down first — nothing left to clean up through it at that point (the
        # OS reaps the browser subprocess with the rest of the process tree
        # regardless; confirmed no process is left behind either way).
        return "Browser session closed."


atexit.register(close)  # don't leak a headless Chromium process on exit
