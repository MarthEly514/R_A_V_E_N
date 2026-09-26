"""Linux desktop control: .desktop files + gtk-launch/xdg-open, pkill, and
the GNOME "Window Calls" extension over D-Bus (gdbus)."""
import ast
import json
import shutil
import subprocess
from pathlib import Path

from raven.platforms import Backend, WindowControlError

APP_DIRS = [
    Path("/usr/share/applications"),
    Path("/usr/local/share/applications"),
    Path.home() / ".local/share/applications",
    Path("/var/lib/flatpak/exports/share/applications"),  # system-wide flatpak installs
    Path.home() / ".local/share/flatpak/exports/share/applications",  # per-user flatpak installs
    Path("/var/lib/snapd/desktop/applications"),  # snap installs
]

# Some single-instance apps default their CLI to opening a brand-new window per
# invocation rather than reusing the one already open. Extra args listed here
# (keyed by .desktop id) fix that, so R.A.V.E.N opening a file on your behalf
# doesn't pile up windows. Add an entry here if another app needs the same fix.
REUSE_WINDOW_ARGS = {
    "code": ["-r"],  # VS Code: -r/--reuse-window, opens into the last active window
}


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


def _gtk_launch(app_id: str, *args: str) -> None:
    subprocess.Popen(
        ["gtk-launch", app_id, *args],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


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
        capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
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


class LinuxBackend(Backend):
    name = "Linux"

    def installed_apps(self) -> dict[str, str]:
        return _installed_apps()

    def launch(self, app_id: str, target: str | None = None) -> str | None:
        extra_args = REUSE_WINDOW_ARGS.get(app_id) if target else None
        binary = _binary_name(app_id) if extra_args else None
        if extra_args and binary and shutil.which(binary):
            # Bypass gtk-launch's %F template substitution here: it's undocumented
            # whether a non-file flag like "-r" survives that substitution alongside
            # the target, so invoke the real binary directly for a guaranteed argv.
            subprocess.Popen(
                [binary, *extra_args, target],
                start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return None
        if not shutil.which("gtk-launch"):
            return "Can't launch apps: gtk-launch is not installed."
        _gtk_launch(app_id, *([target] if target else []))
        return None

    def open_default(self, path: str) -> str | None:
        if not shutil.which("xdg-open"):
            return "Can't open files: xdg-open is not installed."
        subprocess.Popen(
            ["xdg-open", path],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return None

    def close_app(self, app_id: str, display_name: str) -> str:
        binary = _binary_name(app_id)
        if not binary:
            return f"Couldn't determine the process for {display_name.title()}."
        result = subprocess.run(["pkill", "-f", binary], capture_output=True, text=True,
                                stdin=subprocess.DEVNULL)
        if result.returncode == 0:
            return f"Closed {display_name.title()} (all of its windows)."
        if result.returncode == 1:
            return f"{display_name.title()} does not appear to be running."
        return f"Couldn't close {display_name.title()}: {result.stderr.strip() or 'pkill error'}"

    def list_windows(self) -> list[dict]:
        try:
            windows = json.loads(_window_calls("List"))
        except (RuntimeError, subprocess.TimeoutExpired, ValueError) as e:
            raise WindowControlError(str(e)) from e
        return [{"id": w.get("id"), "app": w.get("wm_class", "?"), "title": w.get("title", "")}
                for w in windows]

    def close_window(self, window_id) -> None:
        try:
            _window_calls("Close", str(window_id))
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            raise WindowControlError(str(e)) from e
