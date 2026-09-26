"""Windows desktop control: Start Menu shortcuts, os.startfile, taskkill, and
PowerShell for windows.

Written without access to a Windows machine -- covered by tests that fake
`subprocess`, `os.startfile` and the filesystem, NOT yet confirmed on real
hardware (see WINDOWS_TEST_CHECKLIST.md and DEV_LOG.md, Step 3.57).

Known limits (deliberate, for the first version):
- App discovery reads classic Start Menu shortcuts (.lnk). Microsoft Store /
  UWP apps that don't have one aren't listed.
- Window listing shows each process's MAIN window only, not every window of
  a multi-window app.
- Closing is polite (WM_CLOSE / taskkill without /F): an app with an unsaved-
  changes prompt stays open, which is the safe behaviour.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path, PureWindowsPath

from raven.platforms import Backend, WindowControlError


def _start_menu_dirs() -> list[Path]:
    dirs = []
    for var in ("ProgramData", "APPDATA"):
        base = os.environ.get(var)
        if base:
            dirs.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return dirs


START_MENU_DIRS = _start_menu_dirs()

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_UTF8 = "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"


def _powershell(script: str, env_extra: dict | None = None, timeout: int = 15) -> str:
    """Run a PowerShell snippet, return stdout (UTF-8). Raises WindowControlError."""
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        raise WindowControlError("PowerShell is not available")
    env = {**os.environ, **(env_extra or {})}
    try:
        result = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", _UTF8 + script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, stdin=subprocess.DEVNULL, env=env, creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as e:
        raise WindowControlError("the PowerShell call timed out") from e
    if result.returncode != 0:
        raise WindowControlError((result.stderr or "").strip() or "PowerShell failed")
    return (result.stdout or "").strip()


def _installed_apps() -> dict[str, str]:
    """Lowercase shortcut name -> path of its .lnk, skipping uninstallers."""
    apps = {}
    for directory in START_MENU_DIRS:
        if not directory.is_dir():
            continue
        try:
            shortcuts = sorted(directory.rglob("*.lnk"))
        except OSError:
            continue
        for lnk in shortcuts:
            name = lnk.stem
            if "uninstall" in name.lower():
                continue
            apps[name.lower()] = str(lnk)
    return apps


def _resolve_shortcut(lnk: str) -> tuple[str, str]:
    """(target executable path, arguments) that a .lnk points to. The path is
    passed through an environment variable, never spliced into the script."""
    out = _powershell(
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:RAVEN_LNK);"
        "@{t=$s.TargetPath;a=$s.Arguments}|ConvertTo-Json -Compress",
        env_extra={"RAVEN_LNK": lnk},
    )
    data = json.loads(out)
    return data.get("t") or "", data.get("a") or ""


class WindowsBackend(Backend):
    name = "Windows"

    def installed_apps(self) -> dict[str, str]:
        return _installed_apps()

    def launch(self, app_id: str, target: str | None = None) -> str | None:
        try:
            if not target:
                os.startfile(app_id)  # opens the shortcut exactly as the Start Menu would
                return None
            # ShellExecute can't reliably pass a file argument through a .lnk,
            # so resolve the shortcut to its real executable and start that.
            exe, args = _resolve_shortcut(app_id)
            if not exe:
                return "Couldn't resolve that app's executable, so it can't open a file."
            cmd = [exe] + ([a for a in args.split()] if args else []) + [target]
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
        except (OSError, WindowControlError, ValueError) as e:
            return f"Can't launch apps: {e}."
        return None

    def open_default(self, path: str) -> str | None:
        try:
            os.startfile(path)
        except OSError as e:
            return f"Can't open files: {e}."
        return None

    def close_app(self, app_id: str, display_name: str) -> str:
        label = display_name.title()
        try:
            exe, _ = _resolve_shortcut(app_id)
        except (WindowControlError, ValueError) as e:
            return f"Couldn't close {label}: {e}."
        image = PureWindowsPath(exe).name if exe else ""
        if not image.lower().endswith(".exe"):
            return f"Couldn't determine the process for {label}."
        # No /F: a polite close request, so an app with unsaved work can refuse.
        result = subprocess.run(["taskkill", "/IM", image], capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
        if result.returncode == 0:
            return f"Closed {label} (all of its windows)."
        if result.returncode == 128:  # taskkill: process not found
            return f"{label} does not appear to be running."
        return f"Couldn't close {label}: {(result.stderr or result.stdout).strip() or 'taskkill error'}"

    def list_windows(self) -> list[dict]:
        out = _powershell(
            "Get-Process | Where-Object { $_.MainWindowTitle } | "
            "Select-Object Id,ProcessName,MainWindowTitle | ConvertTo-Json -Compress"
        )
        if not out:
            return []
        try:
            data = json.loads(out)
        except ValueError as e:
            raise WindowControlError("couldn't read PowerShell's window list") from e
        if isinstance(data, dict):  # ConvertTo-Json returns a bare object for a single item
            data = [data]
        return [{"id": w.get("Id"), "app": w.get("ProcessName", "?"),
                 "title": w.get("MainWindowTitle", "")} for w in data]

    def close_window(self, window_id) -> None:
        try:
            pid = int(window_id)
        except (TypeError, ValueError) as e:
            raise WindowControlError("invalid window id") from e
        out = _powershell(f"(Get-Process -Id {pid}).CloseMainWindow()")
        if out.strip().lower() != "true":
            raise WindowControlError("the window did not accept the close request")
