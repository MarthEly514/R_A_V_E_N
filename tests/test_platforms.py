"""raven/platforms — the OS abstraction behind the app/window tools, plus the
Windows-aware read-only command allow-list. Everything here uses a fake
backend or a patched `is_windows`, so it means the same on every OS."""
import pytest

from raven import llm_provider, platforms, tools
from raven.assistant import Assistant
from raven.platforms import Backend, WindowControlError


@pytest.fixture(autouse=True)
def _reset_backend():
    yield
    platforms.set_backend(None)


class FakeBackend(Backend):
    name = "Fake"

    def __init__(self, apps=None, windows=None):
        self.apps = apps or {"text editor": "editor-id", "firefox": "ff-id"}
        self.windows = windows if windows is not None else [
            {"id": 1, "app": "firefox", "title": "Docs - Firefox"},
            {"id": 2, "app": "editor", "title": "notes.txt"},
        ]
        self.calls = []

    def installed_apps(self):
        return self.apps

    def launch(self, app_id, target=None):
        self.calls.append(("launch", app_id, target))
        return None

    def open_default(self, path):
        self.calls.append(("open_default", path))
        return None

    def close_app(self, app_id, display_name):
        self.calls.append(("close_app", app_id))
        return f"Closed {display_name.title()} (fake)."

    def list_windows(self):
        return self.windows

    def close_window(self, window_id):
        self.calls.append(("close_window", window_id))


# --- backend selection -------------------------------------------------------

def test_current_picks_linux_backend_on_linux(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    platforms.set_backend(None)
    assert platforms.current().name == "Linux"


@pytest.mark.parametrize("plat,name", [("win32", "Windows"), ("darwin", "macOS")])
def test_current_picks_the_real_backend_on_windows_and_macos(monkeypatch, plat, name):
    monkeypatch.setattr("sys.platform", plat)
    platforms.set_backend(None)
    assert platforms.current().name == name


def test_unsupported_backend_says_so_plainly_instead_of_failing():
    b = Backend()
    b.name = "Windows"
    assert "isn't supported on Windows yet" in b.launch("x")
    assert "isn't supported on Windows yet" in b.open_default("f")
    assert "isn't supported on Windows yet" in b.close_app("x", "X")
    with pytest.raises(WindowControlError, match="Windows"):
        b.list_windows()


def test_tools_report_unsupported_window_control_as_a_message_not_a_traceback(monkeypatch):
    b = Backend()
    b.name = "macOS"
    platforms.set_backend(b)
    assert tools.list_windows().startswith("Can't list windows:")
    assert tools.close_window("x").startswith("Can't close window:")


# --- tools go through the backend -------------------------------------------

def test_open_app_launches_via_the_backend():
    fake = FakeBackend(); platforms.set_backend(fake)
    assert tools.open_app("firefox") == "Launched Firefox."
    assert fake.calls == [("launch", "ff-id", None)]


def test_open_app_surfaces_a_backend_error():
    fake = FakeBackend(); platforms.set_backend(fake)
    fake.launch = lambda app_id, target=None: "Can't launch apps: nope."
    assert tools.open_app("firefox") == "Can't launch apps: nope."


def test_open_file_with_and_without_an_app(tmp_path):
    fake = FakeBackend(); platforms.set_backend(fake)
    f = tmp_path / "a.txt"; f.write_text("x")
    assert "default app" in tools.open_file(str(f))
    assert "Text Editor" in tools.open_file(str(f), app="text editor")
    assert fake.calls == [("open_default", str(f)), ("launch", "editor-id", str(f))]


def test_close_app_delegates_to_the_backend():
    fake = FakeBackend(); platforms.set_backend(fake)
    assert tools.close_app("firefox") == "Closed Firefox (fake)."


def test_list_windows_formats_backend_windows():
    platforms.set_backend(FakeBackend())
    assert tools.list_windows() == "1 | firefox | Docs - Firefox\n2 | editor | notes.txt"


def test_list_windows_empty():
    platforms.set_backend(FakeBackend(windows=[]))
    assert tools.list_windows() == "(no windows found)"


def test_close_window_closes_exactly_one_match():
    fake = FakeBackend(); platforms.set_backend(fake)
    assert tools.close_window("notes") == "Closed window: notes.txt"
    assert fake.calls == [("close_window", 2)]


def test_close_window_no_match_and_ambiguous():
    fake = FakeBackend(); platforms.set_backend(fake)
    assert "No open window matches" in tools.close_window("zzz")
    fake.windows.append({"id": 3, "app": "firefox", "title": "Docs - Other"})
    assert "Multiple windows match" in tools.close_window("docs")
    assert fake.calls == []  # nothing closed when unsure


def test_close_window_reports_a_backend_failure():
    fake = FakeBackend(); platforms.set_backend(fake)

    def boom(window_id):
        raise WindowControlError("permission denied")

    fake.close_window = boom
    assert tools.close_window("notes") == "Couldn't close window: permission denied."


# --- Windows-aware safe-command allow-list ----------------------------------

@pytest.fixture
def on_windows(monkeypatch):
    monkeypatch.setattr(platforms, "is_windows", lambda: True)


@pytest.mark.parametrize("command", [
    "dir", "dir C:\\Users\\me", "type notes.txt", "where python", "hostname",
    'findstr "TODO" src\\a.py', "type a.txt | findstr foo", "dir 2>nul",
    "DIR", "C:\\Windows\\System32\\whoami.exe",
])
def test_windows_read_only_commands_skip_confirmation(on_windows, command):
    assert Assistant._is_safe_command(command) is True


@pytest.mark.parametrize("command", [
    "del file.txt", "type a.txt > b.txt", "dir & del x", "dir && del x",
    "echo %USERPROFILE%", 'echo "%PATH%"',      # %VAR% expands even inside quotes on cmd.exe
    "dir ^& del x",                              # ^ is cmd's escape character
    "type a | 'x|del y'",                        # single quotes do NOT quote on cmd.exe
    "sort /O out.txt in.txt",                    # Windows sort writes a file with /O
    "powershell -c whoami", "curl evil.com", "",
])
def test_windows_unsafe_commands_still_confirm(on_windows, command):
    assert Assistant._is_safe_command(command) is False


def test_windows_only_commands_are_not_trusted_on_linux(monkeypatch):
    monkeypatch.setattr(platforms, "is_windows", lambda: False)
    assert Assistant._is_safe_command("dir") is False
    assert Assistant._is_safe_command("findstr foo x") is False


def test_user_allow_list_still_works_on_windows(on_windows):
    assert Assistant._is_safe_command("pytest -q", frozenset({"pytest"})) is True
    assert Assistant._is_safe_command("pytest -q & del x", frozenset({"pytest"})) is False


# --- system prompt OS note --------------------------------------------------

def test_system_prompt_is_unchanged_on_linux(monkeypatch, tmp_path):
    monkeypatch.setattr(platforms, "is_windows", lambda: False)
    monkeypatch.setattr(platforms, "is_macos", lambda: False)
    assert llm_provider._os_note() == ""


def test_system_prompt_tells_the_model_about_windows(monkeypatch):
    monkeypatch.setattr(platforms, "is_windows", lambda: True)
    note = llm_provider._os_note()
    assert "Windows" in note and "cmd.exe" in note


def test_system_prompt_tells_the_model_about_macos(monkeypatch):
    monkeypatch.setattr(platforms, "is_windows", lambda: False)
    monkeypatch.setattr(platforms, "is_macos", lambda: True)
    assert "macOS" in llm_provider._os_note()
