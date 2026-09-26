"""The macOS and Windows backends. Written without access to either OS, so
these fake `subprocess`, `os.startfile` and the filesystem: they prove the
logic (commands built, output parsed, errors reported), NOT that the real
osascript/PowerShell/taskkill behave as assumed -- that needs real machines."""
import json
import subprocess
import types

import pytest

from raven.platforms import WindowControlError, macos, windows


def _proc(stdout="", stderr="", returncode=0):
    return types.SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


class Runner:
    """Records subprocess.run calls; returns queued results (or one default)."""
    def __init__(self, *results):
        self.results = list(results) or [_proc()]
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


class Popper:
    def __init__(self, exc=None):
        self.calls, self.exc = [], exc

    def __call__(self, cmd, **kwargs):
        if self.exc:
            raise self.exc
        self.calls.append(cmd)


# =========================== macOS ==========================================

@pytest.fixture
def mac(monkeypatch, tmp_path):
    apps = tmp_path / "Applications"
    apps.mkdir()
    for name in ("Safari", "Visual Studio Code", "Notes"):
        (apps / f"{name}.app").mkdir()
    (apps / "notanapp.txt").write_text("x")
    monkeypatch.setattr(macos, "APP_DIRS", [apps, tmp_path / "missing"])
    return macos.MacBackend(), apps


def test_mac_lists_app_bundles_only(mac):
    backend, apps = mac
    assert backend.installed_apps() == {
        "safari": str(apps / "Safari.app"),
        "visual studio code": str(apps / "Visual Studio Code.app"),
        "notes": str(apps / "Notes.app"),
    }


def test_mac_launch_uses_open_dash_a(mac, monkeypatch):
    backend, apps = mac
    p = Popper(); monkeypatch.setattr(macos.subprocess, "Popen", p)
    assert backend.launch(str(apps / "Safari.app")) is None
    assert p.calls == [["open", "-a", str(apps / "Safari.app")]]


def test_mac_launch_with_a_target(mac, monkeypatch):
    backend, apps = mac
    p = Popper(); monkeypatch.setattr(macos.subprocess, "Popen", p)
    backend.launch(str(apps / "Notes.app"), "/tmp/a.txt")
    assert p.calls == [["open", "-a", str(apps / "Notes.app"), "/tmp/a.txt"]]


def test_mac_open_default_and_failure(mac, monkeypatch):
    backend, _ = mac
    p = Popper(); monkeypatch.setattr(macos.subprocess, "Popen", p)
    assert backend.open_default("/tmp/a.txt") is None and p.calls == [["open", "/tmp/a.txt"]]
    monkeypatch.setattr(macos.subprocess, "Popen", Popper(OSError("no open")))
    assert "no open" in backend.open_default("/tmp/a.txt")


def test_mac_close_app_checks_running_first_so_it_never_launches_a_closed_app(mac, monkeypatch):
    backend, apps = mac
    r = Runner(_proc("false")); monkeypatch.setattr(macos.subprocess, "run", r)
    msg = backend.close_app(str(apps / "Safari.app"), "safari")
    assert "does not appear to be running" in msg
    assert len(r.calls) == 1 and "is running" in r.calls[0][0][2]  # never sent `quit`


def test_mac_close_app_quits_a_running_app(mac, monkeypatch):
    backend, apps = mac
    r = Runner(_proc("true"), _proc("")); monkeypatch.setattr(macos.subprocess, "run", r)
    msg = backend.close_app(str(apps / "Safari.app"), "safari")
    assert msg == "Closed Safari (all of its windows)."
    assert r.calls[1][0] == ["osascript", "-e", 'tell application "Safari" to quit']


def test_mac_applescript_strings_are_escaped():
    assert macos._as_string('a"b\\c') == 'a\\"b\\\\c'


def test_mac_permission_denied_gives_an_actionable_message(mac, monkeypatch):
    backend, _ = mac
    monkeypatch.setattr(macos.subprocess, "run", Runner(_proc(stderr="... not allowed assistive access. (-1719)", returncode=1)))
    with pytest.raises(WindowControlError, match="Accessibility"):
        backend.list_windows()


def test_mac_list_windows_parses_and_ids_are_reused_by_close(mac, monkeypatch):
    backend, _ = mac
    out = "Safari\tApple\nNotes\tShopping list\n"
    r = Runner(_proc(out), _proc("")); monkeypatch.setattr(macos.subprocess, "run", r)
    windows_ = backend.list_windows()
    assert windows_ == [{"id": 0, "app": "Safari", "title": "Apple"},
                        {"id": 1, "app": "Notes", "title": "Shopping list"}]
    backend.close_window(1)
    script = r.calls[1][0][2]
    assert 'process "Notes"' in script and 'window "Shopping list"' in script and "AXCloseButton" in script


def test_mac_close_window_before_listing_is_a_clear_error(mac):
    backend, _ = mac
    with pytest.raises(WindowControlError, match="list_windows"):
        backend.close_window(3)


def test_mac_missing_osascript(mac, monkeypatch):
    backend, _ = mac

    def boom(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(macos.subprocess, "run", boom)
    with pytest.raises(WindowControlError, match="osascript"):
        backend.list_windows()


# =========================== Windows ========================================

@pytest.fixture
def win(monkeypatch, tmp_path):
    menu = tmp_path / "Programs"
    (menu / "Mozilla").mkdir(parents=True)
    (menu / "Google Chrome.lnk").write_text("x")
    (menu / "Mozilla" / "Firefox.lnk").write_text("x")
    (menu / "Uninstall Firefox.lnk").write_text("x")
    (menu / "readme.txt").write_text("x")
    monkeypatch.setattr(windows, "START_MENU_DIRS", [menu, tmp_path / "missing"])
    monkeypatch.setattr(windows.shutil, "which", lambda name: "C:\\ps\\powershell.exe" if name == "powershell" else None)
    return windows.WindowsBackend(), menu


def test_windows_lists_start_menu_shortcuts_recursively_skipping_uninstallers(win):
    backend, menu = win
    assert backend.installed_apps() == {
        "google chrome": str(menu / "Google Chrome.lnk"),
        "firefox": str(menu / "Mozilla" / "Firefox.lnk"),
    }


def test_windows_launch_uses_startfile_for_the_shortcut(win, monkeypatch):
    backend, menu = win
    opened = []
    monkeypatch.setattr(windows.os, "startfile", opened.append, raising=False)
    assert backend.launch(str(menu / "Google Chrome.lnk")) is None
    assert opened == [str(menu / "Google Chrome.lnk")]


def test_windows_launch_with_target_resolves_the_shortcut_then_starts_the_exe(win, monkeypatch):
    backend, menu = win
    r = Runner(_proc(json.dumps({"t": "C:\\Chrome\\chrome.exe", "a": "--profile-directory=Default"})))
    monkeypatch.setattr(windows.subprocess, "run", r)
    p = Popper(); monkeypatch.setattr(windows.subprocess, "Popen", p)
    assert backend.launch(str(menu / "Google Chrome.lnk"), "C:\\docs\\a.html") is None
    assert p.calls == [["C:\\Chrome\\chrome.exe", "--profile-directory=Default", "C:\\docs\\a.html"]]
    # the shortcut path travels via the environment, never spliced into the script:
    assert r.calls[0][1]["env"]["RAVEN_LNK"] == str(menu / "Google Chrome.lnk")
    assert str(menu) not in r.calls[0][0][-1]


def test_windows_launch_with_target_but_unresolvable_shortcut(win, monkeypatch):
    backend, menu = win
    monkeypatch.setattr(windows.subprocess, "run", Runner(_proc(json.dumps({"t": "", "a": ""}))))
    assert "Couldn't resolve" in backend.launch(str(menu / "Google Chrome.lnk"), "x")


def test_windows_open_default(win, monkeypatch):
    backend, _ = win
    opened = []
    monkeypatch.setattr(windows.os, "startfile", opened.append, raising=False)
    assert backend.open_default("C:\\a.txt") is None and opened == ["C:\\a.txt"]

    def boom(path):
        raise OSError("no association")

    monkeypatch.setattr(windows.os, "startfile", boom, raising=False)
    assert "no association" in backend.open_default("C:\\a.txt")


def test_windows_close_app_uses_a_polite_taskkill(win, monkeypatch):
    backend, menu = win
    r = Runner(_proc(json.dumps({"t": "C:\\Chrome\\chrome.exe", "a": ""})), _proc("SUCCESS"))
    monkeypatch.setattr(windows.subprocess, "run", r)
    msg = backend.close_app(str(menu / "Google Chrome.lnk"), "google chrome")
    assert msg == "Closed Google Chrome (all of its windows)."
    assert r.calls[1][0] == ["taskkill", "/IM", "chrome.exe"]  # no /F


def test_windows_close_app_not_running(win, monkeypatch):
    backend, menu = win
    r = Runner(_proc(json.dumps({"t": "C:\\Chrome\\chrome.exe", "a": ""})), _proc(returncode=128))
    monkeypatch.setattr(windows.subprocess, "run", r)
    assert "does not appear to be running" in backend.close_app(str(menu / "Google Chrome.lnk"), "google chrome")


def test_windows_close_app_without_an_exe(win, monkeypatch):
    backend, menu = win
    monkeypatch.setattr(windows.subprocess, "run", Runner(_proc(json.dumps({"t": "", "a": ""}))))
    assert "Couldn't determine the process" in backend.close_app(str(menu / "Google Chrome.lnk"), "google chrome")


def test_windows_list_windows_many_and_single(win, monkeypatch):
    backend, _ = win
    many = json.dumps([{"Id": 10, "ProcessName": "chrome", "MainWindowTitle": "Docs"},
                       {"Id": 11, "ProcessName": "notepad", "MainWindowTitle": "a.txt"}])
    monkeypatch.setattr(windows.subprocess, "run", Runner(_proc(many)))
    assert backend.list_windows() == [{"id": 10, "app": "chrome", "title": "Docs"},
                                      {"id": 11, "app": "notepad", "title": "a.txt"}]
    single = json.dumps({"Id": 5, "ProcessName": "calc", "MainWindowTitle": "Calculator"})
    monkeypatch.setattr(windows.subprocess, "run", Runner(_proc(single)))
    assert backend.list_windows() == [{"id": 5, "app": "calc", "title": "Calculator"}]
    monkeypatch.setattr(windows.subprocess, "run", Runner(_proc("")))
    assert backend.list_windows() == []


def test_windows_list_windows_bad_output(win, monkeypatch):
    backend, _ = win
    monkeypatch.setattr(windows.subprocess, "run", Runner(_proc("not json")))
    with pytest.raises(WindowControlError):
        backend.list_windows()


def test_windows_close_window_uses_closemainwindow_with_an_integer_pid(win, monkeypatch):
    backend, _ = win
    r = Runner(_proc("True")); monkeypatch.setattr(windows.subprocess, "run", r)
    backend.close_window(10)
    assert "(Get-Process -Id 10).CloseMainWindow()" in r.calls[0][0][-1]


def test_windows_close_window_rejects_a_non_integer_id_and_a_refusal(win, monkeypatch):
    backend, _ = win
    with pytest.raises(WindowControlError):
        backend.close_window("10; Remove-Item C:\\")   # would be injection if spliced
    monkeypatch.setattr(windows.subprocess, "run", Runner(_proc("False")))
    with pytest.raises(WindowControlError, match="did not accept"):
        backend.close_window(10)


def test_windows_powershell_missing(win, monkeypatch):
    backend, _ = win
    monkeypatch.setattr(windows.shutil, "which", lambda name: None)
    with pytest.raises(WindowControlError, match="PowerShell"):
        backend.list_windows()


def test_windows_powershell_failure_and_timeout(win, monkeypatch):
    backend, _ = win
    monkeypatch.setattr(windows.subprocess, "run", Runner(_proc(stderr="Access denied", returncode=1)))
    with pytest.raises(WindowControlError, match="Access denied"):
        backend.list_windows()

    def slow(*a, **k):
        raise subprocess.TimeoutExpired("powershell", 15)

    monkeypatch.setattr(windows.subprocess, "run", slow)
    with pytest.raises(WindowControlError, match="timed out"):
        backend.list_windows()
