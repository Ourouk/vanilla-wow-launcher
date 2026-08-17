"""Tests for GUI backend selection in vanilla_wow_launcher.cli.

Only the resolver and startup wiring are exercised here. The Qt backend
runs headless (it never opens a display in these tests). `main()` with no
launcher config opens the first-launch wizard (`_pick_launcher_config`),
which is monkeypatched here.
"""

import json

import pytest

import vanilla_wow_launcher.core.launcher as launcher
from vanilla_wow_launcher import cli

QT_UNAVAILABLE = "Vanilla WoW Launcher needs PySide6 (Qt) to run"


@pytest.fixture
def launcher_file(tmp_path):
    path = tmp_path / "vanilla_wow_launcher.json"
    path.write_text(
        json.dumps({"server": {"base_url": "https://launcher.test"}}),
        encoding="utf-8",
    )
    return str(path)


@pytest.fixture
def no_persisted_config(monkeypatch, tmp_path):
    """Keep auto-discovery from picking up a real per-user config."""
    monkeypatch.setattr(
        launcher, "user_config_path", lambda: str(tmp_path / "none.json")
    )


def test_resolve_backend_default_is_qt(monkeypatch):
    import vanilla_wow_launcher.ui.qt.app as qt_app

    monkeypatch.delenv("VANILLA_WOW_UI_BACKEND", raising=False)
    assert cli.resolve_backend() is qt_app.QtVanillaWoWLauncherApp


def test_resolve_backend_qt_returns_app_class():
    import vanilla_wow_launcher.ui.qt.app as qt_app

    assert cli.resolve_backend("qt") is qt_app.QtVanillaWoWLauncherApp


def test_resolve_backend_pyside6_returns_app_class():
    import vanilla_wow_launcher.ui.qt.app as qt_app

    assert cli.resolve_backend("pyside6") is qt_app.QtVanillaWoWLauncherApp


def test_qt_backend_error_message_is_friendly():
    msg = cli.backend_error_message("qt", ImportError("broken"))
    assert QT_UNAVAILABLE in msg


def test_main_returns_1_when_qt_import_fails(
    monkeypatch, capsys, launcher_file
):
    import sys

    monkeypatch.setenv("VANILLA_WOW_UI_BACKEND", "qt")
    monkeypatch.setitem(sys.modules, "vanilla_wow_launcher.ui.qt.app", None)
    assert cli.main(["--launcher-config", launcher_file]) == 1
    assert QT_UNAVAILABLE in capsys.readouterr().err


def test_unknown_backend_returns_none():
    assert cli.resolve_backend("bogus") is None


def test_main_returns_1_for_unknown_backend(
    monkeypatch, capsys, launcher_file
):
    monkeypatch.setenv("VANILLA_WOW_UI_BACKEND", "bogus")
    assert cli.main(["--launcher-config", launcher_file]) == 1
    assert "Unknown VANILLA_WOW_UI_BACKEND: bogus" in capsys.readouterr().err


def test_main_returns_1_without_launcher_config(
    monkeypatch, capsys, tmp_path, no_persisted_config
):
    monkeypatch.setenv("VANILLA_WOW_UI_BACKEND", "qt")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_pick_launcher_config", lambda: None)
    assert cli.main([]) == 1
    assert "No launcher configuration selected" in capsys.readouterr().err


def test_main_wizard_selection_runs_backend(
    monkeypatch, capsys, launcher_file, tmp_path, no_persisted_config
):
    calls = []

    class FakeQtApp:
        def __init__(self):
            calls.append("constructed")

        def show(self):
            calls.append("shown")

        def run(self):
            calls.append("run")
            return 0

    monkeypatch.setenv("VANILLA_WOW_UI_BACKEND", "qt")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(
        cli,
        "_pick_launcher_config",
        lambda: {"kind": "file", "path": launcher_file},
    )
    monkeypatch.setattr(cli, "resolve_backend", lambda name: FakeQtApp)
    assert cli.main([]) == 0
    assert calls == ["constructed", "shown", "run"]


def test_main_wizard_selection_persists_config(
    monkeypatch, launcher_file, tmp_path
):
    calls = []

    class FakeQtApp:
        def __init__(self):
            calls.append("constructed")

        def show(self):
            calls.append("shown")

        def run(self):
            calls.append("run")
            return 0

    dest = tmp_path / "persisted" / "vanilla_wow_launcher.json"
    monkeypatch.setattr(launcher, "user_config_path", lambda: str(dest))
    monkeypatch.setenv("VANILLA_WOW_UI_BACKEND", "qt")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(
        cli,
        "_pick_launcher_config",
        lambda: {"kind": "file", "path": launcher_file},
    )
    monkeypatch.setattr(cli, "resolve_backend", lambda name: FakeQtApp)
    assert cli.main([]) == 0
    assert dest.exists()
    assert json.loads(dest.read_text()) == {
        "server": {"base_url": "https://launcher.test"}
    }
    assert launcher.config().server_url == "https://launcher.test"


def test_main_wizard_persistence_failure_aborts(
    monkeypatch, capsys, launcher_file, tmp_path
):
    """If saving the imported config fails, startup aborts and the backend is
    never constructed."""
    calls = []

    class FakeQtApp:
        def __init__(self):
            calls.append("constructed")

        def show(self):
            calls.append("shown")

        def run(self):
            calls.append("run")
            return 0

    monkeypatch.setattr(
        launcher, "user_config_path", lambda: str(tmp_path / "none.json")
    )
    monkeypatch.setattr(
        launcher,
        "persist",
        lambda path: ("", "Could not save the launcher configuration: boom"),
    )
    monkeypatch.setenv("VANILLA_WOW_UI_BACKEND", "qt")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(
        cli,
        "_pick_launcher_config",
        lambda: {"kind": "file", "path": launcher_file},
    )
    monkeypatch.setattr(cli, "resolve_backend", lambda name: FakeQtApp)
    assert cli.main([]) == 1
    assert (
        "Could not save the launcher configuration" in capsys.readouterr().err
    )
    assert calls == []


def test_main_explicit_bad_config_never_opens_wizard(
    monkeypatch, capsys, tmp_path
):
    recorder = []
    monkeypatch.setattr(
        cli, "_pick_launcher_config", lambda: recorder.append("called")
    )
    assert cli.main(["--launcher-config", str(tmp_path / "missing.json")]) == 1
    assert "Invalid launcher configuration" in capsys.readouterr().err
    assert recorder == []


def test_main_wizard_qt_import_failure(
    monkeypatch, capsys, tmp_path, no_persisted_config
):
    monkeypatch.setenv("VANILLA_WOW_UI_BACKEND", "qt")
    monkeypatch.chdir(tmp_path)

    def _fail():
        raise ImportError("no qt")

    monkeypatch.setattr(cli, "_pick_launcher_config", _fail)
    assert cli.main([]) == 1
    assert "PySide6" in capsys.readouterr().err


@pytest.mark.parametrize("backend", ["qt", "pyside6"])
def test_main_constructs_shows_and_runs_qt_backend(
    monkeypatch, backend, launcher_file
):
    calls = []

    class FakeQtApp:
        def __init__(self):
            calls.append("constructed")

        def show(self):
            calls.append("shown")

        def run(self):
            calls.append("run")
            return 0

    monkeypatch.setenv("VANILLA_WOW_UI_BACKEND", backend)
    monkeypatch.setattr(cli, "resolve_backend", lambda name: FakeQtApp)
    assert cli.main(["--launcher-config", launcher_file]) == 0
    assert calls == ["constructed", "shown", "run"]
