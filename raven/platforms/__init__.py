"""OS-specific desktop control, behind one interface.

tools.py's app/window tools (open_app, open_file, close_app, list_windows,
close_window, list_apps) are OS-independent in *behaviour* -- fuzzy name
matching, confirmation rules, result messages -- but the mechanism for
"launch an app" or "close one window" is completely different on Linux
(.desktop files, gtk-launch, D-Bus), macOS (.app bundles, `open`, AppleScript)
and Windows (Start Menu shortcuts, taskkill, Win32). Each OS gets a Backend;
tools.py asks `current()` for the right one and never checks sys.platform.

A backend that can't do something returns/raises a clear "not supported"
message rather than guessing -- same posture as the Window Calls extension
already had on Linux ("says so plainly instead of failing silently").
"""
import sys


class WindowControlError(RuntimeError):
    """Window listing/closing isn't available (unsupported OS, missing
    extension or permission). The message is user-facing."""


class Backend:
    """Base backend: everything unsupported. Real backends override what
    their OS can do. Method contracts are what tools.py relies on."""
    name = "unsupported"

    def installed_apps(self) -> dict[str, str]:
        """Lowercase display name -> opaque app id (whatever launch() needs)."""
        return {}

    def launch(self, app_id: str, target: str | None = None) -> str | None:
        """Start an app, optionally opening `target` (file path or URL) in it.
        Returns None on success, else a user-facing error string."""
        return f"Launching apps isn't supported on {self.name} yet."

    def open_default(self, path: str) -> str | None:
        """Open a file with the system default app. None on success, else an error string."""
        return f"Opening files isn't supported on {self.name} yet."

    def close_app(self, app_id: str, display_name: str) -> str:
        """Close every window of a running app; returns the full result message."""
        return f"Closing apps isn't supported on {self.name} yet."

    def list_windows(self) -> list[dict]:
        """Open windows as dicts with keys id, app, title. Raises WindowControlError."""
        raise WindowControlError(f"window control isn't supported on {self.name} yet")

    def close_window(self, window_id) -> None:
        """Close exactly one window by id. Raises WindowControlError."""
        raise WindowControlError(f"window control isn't supported on {self.name} yet")


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_macos() -> bool:
    return sys.platform == "darwin"


def os_name() -> str:
    """Human name of the host OS, for messages and the system prompt."""
    if is_windows():
        return "Windows"
    if is_macos():
        return "macOS"
    return "Linux"


_backend: Backend | None = None


def current() -> Backend:
    """The backend for this OS (created once)."""
    global _backend
    if _backend is None:
        if is_windows():
            from raven.platforms.windows import WindowsBackend
            _backend = WindowsBackend()
        elif is_macos():
            from raven.platforms.macos import MacBackend
            _backend = MacBackend()
        else:
            from raven.platforms.linux import LinuxBackend
            _backend = LinuxBackend()
    return _backend


def set_backend(backend: Backend | None) -> None:
    """Override (or reset with None) the active backend -- for tests."""
    global _backend
    _backend = backend
