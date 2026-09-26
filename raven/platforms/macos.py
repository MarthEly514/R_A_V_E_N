"""macOS desktop control: `.app` bundles, `open`, and AppleScript via osascript.

Written without access to a Mac -- covered by tests that fake `subprocess`
and the filesystem, NOT yet confirmed on real hardware. The real-machine
checklist is in DEV_LOG.md (Step 3.57).

Permissions: listing/closing individual windows uses System Events, which
macOS only allows once the app running R.A.V.E.N (Terminal, iTerm, ...) is
granted Accessibility (System Settings > Privacy & Security > Accessibility).
Without it we say so instead of failing obscurely.
"""
import subprocess
from pathlib import Path

from raven.platforms import Backend, WindowControlError

APP_DIRS = [
    Path("/Applications"),
    Path("/Applications/Utilities"),
    Path("/System/Applications"),
    Path("/System/Applications/Utilities"),
    Path.home() / "Applications",
]

_PERMISSION_HINT = (
    "macOS blocked this -- grant Accessibility permission to your terminal app in "
    "System Settings > Privacy & Security > Accessibility, then try again"
)
# osascript error markers: -1719/-25211 = assistive access denied, -1743 = Apple events not authorized.
_PERMISSION_MARKERS = ("assistive access", "-1719", "-25211", "-1743", "not authorized")


def _as_string(value: str) -> str:
    """Escape a value for use inside an AppleScript double-quoted string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _osascript(script: str, timeout: int = 10) -> str:
    """Run one AppleScript and return stdout. Raises WindowControlError with a
    clear cause on failure (permission denied, osascript missing, timeout)."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as e:
        raise WindowControlError("osascript is not available") from e
    except subprocess.TimeoutExpired as e:
        raise WindowControlError("the AppleScript call timed out") from e
    if result.returncode != 0:
        err = (result.stderr or "").strip()
        if any(m in err.lower() for m in _PERMISSION_MARKERS):
            raise WindowControlError(_PERMISSION_HINT)
        raise WindowControlError(err or "osascript failed")
    return result.stdout.strip()


def _installed_apps() -> dict[str, str]:
    """Lowercase app name -> path of its .app bundle."""
    apps = {}
    for directory in APP_DIRS:
        if not directory.is_dir():
            continue
        try:
            bundles = sorted(directory.glob("*.app"))
        except OSError:
            continue
        for bundle in bundles:
            apps[bundle.stem.lower()] = str(bundle)
    return apps


class MacBackend(Backend):
    name = "macOS"

    def __init__(self):
        # Window ids handed out by the last list_windows() -> (app, title), so
        # close_window can find the same window again (macOS has no stable window ids).
        self._windows: dict[int, tuple[str, str]] = {}

    def installed_apps(self) -> dict[str, str]:
        return _installed_apps()

    def launch(self, app_id: str, target: str | None = None) -> str | None:
        cmd = ["open", "-a", app_id] + ([target] if target else [])
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL)
        except OSError as e:
            return f"Can't launch apps: {e}."
        return None

    def open_default(self, path: str) -> str | None:
        try:
            subprocess.Popen(["open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL)
        except OSError as e:
            return f"Can't open files: {e}."
        return None

    def close_app(self, app_id: str, display_name: str) -> str:
        app = _as_string(Path(app_id).stem)
        label = display_name.title()
        try:
            # `application "X" is running` does NOT launch X. Quitting a
            # not-running app with `tell ... to quit` would launch it first.
            running = _osascript(f'application "{app}" is running')
            if running != "true":
                return f"{label} does not appear to be running."
            _osascript(f'tell application "{app}" to quit')
        except WindowControlError as e:
            return f"Couldn't close {label}: {e}."
        return f"Closed {label} (all of its windows)."

    def list_windows(self) -> list[dict]:
        script = (
            'tell application "System Events"\n'
            '  set out to ""\n'
            '  repeat with p in (every process whose background only is false)\n'
            '    set pname to name of p\n'
            '    try\n'
            '      repeat with w in (every window of p)\n'
            '        set out to out & pname & (ASCII character 9) & (name of w) & linefeed\n'
            '      end repeat\n'
            '    end try\n'
            '  end repeat\n'
            '  return out\n'
            'end tell'
        )
        raw = _osascript(script)
        windows, self._windows = [], {}
        for i, line in enumerate(l for l in raw.splitlines() if l.strip()):
            app, _, title = line.partition("\t")
            self._windows[i] = (app, title)
            windows.append({"id": i, "app": app, "title": title})
        return windows

    def close_window(self, window_id) -> None:
        try:
            app, title = self._windows[int(window_id)]
        except (KeyError, ValueError) as e:
            raise WindowControlError("unknown window -- call list_windows first") from e
        script = (
            f'tell application "System Events" to tell process "{_as_string(app)}" to '
            f'click (first button of window "{_as_string(title)}" whose subrole is "AXCloseButton")'
        )
        _osascript(script)
