"""Unit tests for the umu-launcher service (services/umu.py)."""

import os
import signal
import unittest.mock as mock

import pytest

import vanilla_wow_launcher.services.umu as umu
from vanilla_wow_launcher.core import platform_support


@pytest.fixture(autouse=True)
def _umu_env(monkeypatch, tmp_path):
    """Pin the data dir and the Steam compatibility-tools root per test, so
    nothing touches the real user's filesystem."""
    monkeypatch.setattr(
        platform_support, "data_dir", lambda: str(tmp_path / "data")
    )
    monkeypatch.setattr(
        umu, "_COMPAT_TOOLS_DIRS", (str(tmp_path / "compat-tools"),)
    )
    (tmp_path / "compat-tools").mkdir(parents=True, exist_ok=True)


# ── find_umu ────────────────────────────────────────────────────────────


def test_find_umu_on_path(monkeypatch):
    monkeypatch.setattr(umu.shutil, "which", lambda name: "/usr/bin/umu-run")
    assert umu.find_umu() == "/usr/bin/umu-run"


def test_find_umu_falls_back_to_local_bin(monkeypatch, tmp_path):
    monkeypatch.setattr(umu.shutil, "which", lambda name: None)
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    runner = local_bin / "umu-run"
    runner.write_text("#!/bin/sh\n")
    runner.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert umu.find_umu() == str(runner)


def test_find_umu_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(umu.shutil, "which", lambda name: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert umu.find_umu() == ""


def test_umu_available_reflects_find(monkeypatch):
    monkeypatch.setattr(umu, "find_umu", lambda: "/usr/bin/umu-run")
    assert umu.umu_available() is True
    monkeypatch.setattr(umu, "find_umu", lambda: "")
    assert umu.umu_available() is False


# ── resolve_proton ──────────────────────────────────────────────────────


def test_resolve_proton_passes_through_paths():
    assert umu.resolve_proton("~/GE-Proton9-4") == "~/GE-Proton9-4"
    assert umu.resolve_proton("/opt/Proton") == "/opt/Proton"
    assert umu.resolve_proton("") == ""


def test_resolve_proton_picks_highest_codename(monkeypatch, tmp_path):
    root = tmp_path / "compat-tools"
    for name in ("GE-Proton8-31", "GE-Proton9-4", "GE-Proton9-18"):
        (root / name).mkdir()
    assert umu.resolve_proton("GE-Proton") == str(root / "GE-Proton9-18")


def test_resolve_proton_skips_non_matching_dirs(monkeypatch, tmp_path):
    root = tmp_path / "compat-tools"
    (root / "UMU-Proton").mkdir()
    assert umu.resolve_proton("GE-Proton") == "GE-Proton"


def test_resolve_proton_exact_match_wins(monkeypatch, tmp_path):
    root = tmp_path / "compat-tools"
    (root / "GE-Proton9-5").mkdir()
    (root / "GE-Proton9-50").mkdir()
    assert umu.resolve_proton("GE-Proton9-5") == str(root / "GE-Proton9-5")


# ── list_protons / default_proton / available_protons ──────────────────


def test_list_protons_newest_first(monkeypatch, tmp_path):
    root = tmp_path / "compat-tools"
    for name in ("GE-Proton9-4", "GE-Proton9-18", "UMU-Proton-9-17"):
        (root / name).mkdir()
    found = umu.list_protons()
    assert found[0] == "GE-Proton9-18"
    assert set(found) == {
        "GE-Proton9-4",
        "GE-Proton9-18",
        "UMU-Proton-9-17",
    }


def test_list_protons_dedupes_and_ignores_files(monkeypatch, tmp_path):
    root = tmp_path / "compat-tools"
    (root / "GE-Proton9-4").mkdir()
    (root / "not-a-proton.txt").write_text("x")
    assert umu.list_protons() == ["GE-Proton9-4"]


def test_default_proton_prefers_newest_build(monkeypatch, tmp_path):
    root = tmp_path / "compat-tools"
    (root / "GE-Proton9-4").mkdir()
    (root / "UMU-Proton-9-17").mkdir()
    assert umu.default_proton() == "UMU-Proton-9-17"


def test_default_proton_falls_back_to_umu_codename(monkeypatch, tmp_path):
    assert umu.default_proton() == "UMU-Proton"


def test_available_protons_includes_codenames(monkeypatch, tmp_path):
    root = tmp_path / "compat-tools"
    (root / "GE-Proton9-4").mkdir()
    result = umu.available_protons()
    assert "GE-Proton9-4" in result
    for codename in umu.PROTON_CODENAMES:
        assert codename in result
    # A build and a codename for the same family aren't duplicated.
    assert result.count("GE-Proton") == 1


# ── compute_wine_prefix ───────────────────────────────────────────────


def test_compute_wine_prefix_under_data_dir(tmp_path):
    prefix = umu.compute_wine_prefix()
    assert prefix == str(tmp_path / "data" / "wineprefix")
    assert os.path.isdir(prefix)


# ── build_env ───────────────────────────────────────────────────────────


def test_build_env_sets_umu_contract(monkeypatch, tmp_path):
    prefix = umu.compute_wine_prefix()
    env = umu.build_env("GE-Proton", "umu-vanilla-wow")
    assert env["WINEPREFIX"] == prefix
    assert env["PROTONPATH"] == "GE-Proton"
    assert env["GAMEID"] == "umu-vanilla-wow"
    assert env["STORE"] == "none"
    assert env.get("HOME")  # inherits the rest of the environment


def test_build_env_resolves_proton_codename(monkeypatch, tmp_path):
    root = tmp_path / "compat-tools"
    (root / "GE-Proton9-4").mkdir()
    env = umu.build_env("GE-Proton", "id")
    assert env["PROTONPATH"] == str(root / "GE-Proton9-4")


def test_build_env_dxvk_d3d8_renderer(monkeypatch, tmp_path):
    env = umu.build_env("UMU-Proton", "id", renderer="dxvk-d3d8")
    assert env.get("PROTON_DXVK_D3D8") == "1"
    assert "PROTON_USE_WINED3D" not in env


def test_build_env_wined3d_opengl_renderer(monkeypatch, tmp_path):
    env = umu.build_env("UMU-Proton", "id", renderer="wined3d-opengl")
    assert env.get("PROTON_USE_WINED3D") == "1"
    assert "PROTON_DXVK_D3D8" not in env


def test_build_env_auto_renderer_sets_no_override(monkeypatch, tmp_path):
    env = umu.build_env("UMU-Proton", "id", renderer="auto")
    assert "PROTON_DXVK_D3D8" not in env
    assert "PROTON_USE_WINED3D" not in env


# ── launch renderer env ───────────────────────────────────────────────


def test_launch_passes_renderer_env(monkeypatch, tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    exe = game / "WoW.exe"
    exe.write_text("")
    popen = mock.Mock()
    popen.return_value.pid = 7
    monkeypatch.setattr(umu.subprocess, "Popen", popen)
    monkeypatch.setattr(umu.os, "getpgid", lambda pid: 7)
    monkeypatch.setattr(umu, "find_umu", lambda: "/usr/bin/umu-run")

    umu.launch(
        str(game),
        str(exe),
        proton="UMU-Proton",
        renderer="wined3d-opengl",
    )

    _, kwargs = popen.call_args
    assert kwargs["env"]["PROTON_USE_WINED3D"] == "1"


def test_launch_forwards_wayland_env(monkeypatch, tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    exe = game / "WoW.exe"
    exe.write_text("")
    popen = mock.Mock()
    popen.return_value.pid = 7
    monkeypatch.setattr(umu.subprocess, "Popen", popen)
    monkeypatch.setattr(umu.os, "getpgid", lambda pid: 7)
    monkeypatch.setattr(umu, "find_umu", lambda: "/usr/bin/umu-run")

    umu.launch(str(game), str(exe), proton="UMU-Proton", wayland=True)

    _, kwargs = popen.call_args
    assert kwargs["env"].get("PROTON_ENABLE_WAYLAND") == "1"


def test_launch_omits_wayland_env_when_disabled(monkeypatch, tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    exe = game / "WoW.exe"
    exe.write_text("")
    popen = mock.Mock()
    popen.return_value.pid = 7
    monkeypatch.setattr(umu.subprocess, "Popen", popen)
    monkeypatch.setattr(umu.os, "getpgid", lambda pid: 7)
    monkeypatch.setattr(umu, "find_umu", lambda: "/usr/bin/umu-run")

    umu.launch(str(game), str(exe), proton="UMU-Proton", wayland=False)

    _, kwargs = popen.call_args
    assert "PROTON_ENABLE_WAYLAND" not in kwargs["env"]


# ── launch ──────────────────────────────────────────────────────────────


def test_launch_spawns_umu_detached(monkeypatch, tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    exe = game / "WoW.exe"
    exe.write_text("")
    popen = mock.Mock()
    popen.return_value.pid = 4242
    monkeypatch.setattr(umu.subprocess, "Popen", popen)
    monkeypatch.setattr(umu.os, "getpgid", lambda pid: 9999)
    monkeypatch.setattr(umu, "find_umu", lambda: "/usr/bin/umu-run")

    pid, pgid, proc = umu.launch(
        str(game), str(exe), proton="GE-Proton9-4", game_id="umu-vanilla-wow"
    )

    assert pid == 4242
    assert pgid == 9999
    assert proc is popen.return_value
    args, kwargs = popen.call_args
    assert args[0] == ["/usr/bin/umu-run", str(exe)]
    assert kwargs["cwd"] == str(game)
    assert kwargs["start_new_session"] is True
    assert kwargs["close_fds"] is True
    assert kwargs["env"]["PROTONPATH"] == "GE-Proton9-4"
    assert kwargs["env"]["GAMEID"] == "umu-vanilla-wow"


def test_launch_falls_back_to_pid_when_pgid_unavailable(monkeypatch, tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    exe = game / "WoW.exe"
    exe.write_text("")
    popen = mock.Mock()
    popen.return_value.pid = 4242
    monkeypatch.setattr(umu.subprocess, "Popen", popen)
    monkeypatch.setattr(
        umu.os, "getpgid", mock.Mock(side_effect=OSError("no such process"))
    )
    monkeypatch.setattr(umu, "find_umu", lambda: "/usr/bin/umu-run")

    pid, pgid, _ = umu.launch(str(game), str(exe))

    assert pid == 4242
    assert pgid == 4242


def test_launch_uses_custom_binary(monkeypatch, tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    exe = game / "WoW.exe"
    exe.write_text("")
    popen = mock.Mock()
    popen.return_value.pid = 1
    monkeypatch.setattr(umu.subprocess, "Popen", popen)
    monkeypatch.setattr(umu.os, "getpgid", lambda pid: 1)
    monkeypatch.setattr(umu, "find_umu", lambda: "")

    pid, pgid, _ = umu.launch(str(game), str(exe), umu_binary="/opt/umu-run")

    args, _ = popen.call_args
    assert args[0] == ["/opt/umu-run", str(exe)]
    assert pid == 1
    assert pgid == 1


def test_launch_raises_when_umu_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(umu, "find_umu", lambda: "")
    with pytest.raises(RuntimeError, match="umu-run not found"):
        umu.launch(str(tmp_path), str(tmp_path / "WoW.exe"))


# ── kill_game ───────────────────────────────────────────────────────────


def test_kill_game_escalates_to_sigkill(monkeypatch):
    kills = []
    monkeypatch.setattr(
        umu.os, "killpg", lambda pgid, sig: kills.append(("pg", pgid, sig))
    )
    monkeypatch.setattr(
        umu.os, "kill", lambda pid, sig: kills.append(("p", pid, sig))
    )
    monkeypatch.setattr(umu.time, "sleep", lambda s: None)

    umu.kill_game(4242, 9999, grace=0.01)

    assert ("pg", 9999, signal.SIGTERM) in kills
    assert ("p", 4242, 0) in kills  # liveness probe
    assert ("pg", 9999, signal.SIGKILL) in kills


def test_kill_game_noop_when_process_already_gone(monkeypatch):
    kills = []
    monkeypatch.setattr(
        umu.os, "killpg", mock.Mock(side_effect=ProcessLookupError)
    )
    monkeypatch.setattr(umu.os, "kill", lambda pid, sig: kills.append(sig))

    umu.kill_game(4242, 9999)

    assert kills == []  # never probed after SIGTERM missed


def test_kill_game_returns_early_when_process_exits_on_sigterm(monkeypatch):
    monkeypatch.setattr(umu.os, "killpg", lambda pgid, sig: None)
    # Process dies right after SIGTERM: the first liveness probe raises.
    monkeypatch.setattr(
        umu.os, "kill", mock.Mock(side_effect=ProcessLookupError)
    )
    monkeypatch.setattr(umu.time, "sleep", lambda s: None)

    umu.kill_game(4242, 9999, grace=5.0)  # no SIGKILL despite long grace


def test_kill_game_noop_off_linux(monkeypatch):
    monkeypatch.setattr(platform_support, "is_linux", lambda: False)
    killed = []
    monkeypatch.setattr(umu.os, "killpg", lambda pgid, sig: killed.append(sig))
    monkeypatch.setattr(umu.os, "kill", lambda pid, sig: killed.append(sig))

    umu.kill_game(4242, 9999)

    assert killed == []


# ── Linux feature detection ─────────────────────────────────────────────


def test_find_gamemoderun_probes_path(monkeypatch):
    monkeypatch.setattr(
        umu.shutil, "which", lambda name: "/usr/bin/gamemoderun"
    )
    assert umu.find_gamemoderun() == "/usr/bin/gamemoderun"
    monkeypatch.setattr(umu.shutil, "which", lambda name: "")
    assert umu.find_gamemoderun() == ""


def test_is_wayland_session_reads_env(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert umu.is_wayland_session() is True
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", ":0")
    assert umu.is_wayland_session() is True
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    assert umu.is_wayland_session() is False


def test_scan_linux_features(monkeypatch):
    monkeypatch.setattr(
        umu, "find_gamemoderun", lambda: "/usr/bin/gamemoderun"
    )
    monkeypatch.setattr(umu, "is_wayland_session", lambda: True)
    assert umu.scan_linux_features() == {
        "gamemode_available": True,
        "wayland_session": True,
    }


# ── build_env / launch: Wayland + GameMode ─────────────────────────────


def test_build_env_wayland_sets_proton_flag(monkeypatch, tmp_path):
    env = umu.build_env("UMU-Proton", "id", wayland=True)
    assert env.get("PROTON_ENABLE_WAYLAND") == "1"
    env = umu.build_env("UMU-Proton", "id", wayland=False)
    assert "PROTON_ENABLE_WAYLAND" not in env


def test_launch_prepends_gamemoderun(monkeypatch, tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    exe = game / "WoW.exe"
    exe.write_text("")
    popen = mock.Mock()
    popen.return_value.pid = 7
    monkeypatch.setattr(umu.subprocess, "Popen", popen)
    monkeypatch.setattr(umu.os, "getpgid", lambda pid: 7)
    monkeypatch.setattr(umu, "find_umu", lambda: "/usr/bin/umu-run")
    monkeypatch.setattr(
        umu, "find_gamemoderun", lambda: "/usr/bin/gamemoderun"
    )

    umu.launch(str(game), str(exe), gamemode=True)

    args, _ = popen.call_args
    assert args[0] == ["/usr/bin/gamemoderun", "/usr/bin/umu-run", str(exe)]


def test_launch_skips_gamemoderun_when_absent(monkeypatch, tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    exe = game / "WoW.exe"
    exe.write_text("")
    popen = mock.Mock()
    popen.return_value.pid = 7
    monkeypatch.setattr(umu.subprocess, "Popen", popen)
    monkeypatch.setattr(umu.os, "getpgid", lambda pid: 7)
    monkeypatch.setattr(umu, "find_umu", lambda: "/usr/bin/umu-run")
    monkeypatch.setattr(umu, "find_gamemoderun", lambda: "")

    umu.launch(str(game), str(exe), gamemode=True)

    args, _ = popen.call_args
    assert args[0] == ["/usr/bin/umu-run", str(exe)]
