"""Unit tests for platform detection and cross-platform helpers."""

import os
import subprocess
import sys
import unittest.mock as mock

import pytest

from vanilla_wow_launcher.core.platform_support import (
    cache_dir,
    can_launch_client,
    can_manage_antivirus,
    config_dir,
    data_dir,
    default_out_dir,
    is_linux,
    is_macos,
    is_windows,
    open_folder,
)


@pytest.fixture
def fake_platform(monkeypatch):
    """Set sys.platform for the duration of a test."""

    def _set(platform):
        monkeypatch.setattr(sys, "platform", platform)

    return _set


# ── detection ───────────────────────────────────────────────────────────────


def test_is_windows(fake_platform):
    fake_platform("win32")
    assert is_windows()
    assert not is_macos()
    assert not is_linux()


def test_is_macos(fake_platform):
    fake_platform("darwin")
    assert is_macos()
    assert not is_windows()


def test_is_linux(fake_platform):
    fake_platform("linux")
    assert is_linux()
    assert not is_windows()


# ── capabilities (option 2: generic only on non-Windows) ────────────────────


def test_capabilities_windows(fake_platform):
    fake_platform("win32")
    assert can_launch_client()
    assert can_manage_antivirus()


def test_capabilities_macos(fake_platform):
    fake_platform("darwin")
    assert not can_launch_client()
    assert not can_manage_antivirus()


def test_capabilities_linux_without_umu(fake_platform, monkeypatch):
    fake_platform("linux")
    monkeypatch.setattr(
        "vanilla_wow_launcher.services.umu.umu_available", lambda: False
    )
    assert not can_launch_client()
    assert not can_manage_antivirus()


def test_capabilities_linux_with_umu(fake_platform, monkeypatch):
    fake_platform("linux")
    monkeypatch.setattr(
        "vanilla_wow_launcher.services.umu.umu_available", lambda: True
    )
    assert can_launch_client()
    assert not can_manage_antivirus()


# ── config/cache dirs ───────────────────────────────────────────────────────


def test_config_dir_windows_uses_appdata(fake_platform, monkeypatch, tmp_path):
    fake_platform("win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    assert config_dir() == str(tmp_path / "roaming" / "VanillaWoWLauncher")


def test_config_dir_windows_falls_back_to_userprofile(
    fake_platform, monkeypatch
):
    fake_platform("win32")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("USERPROFILE", os.path.join("C:\\", "Users", "user"))
    assert config_dir() == os.path.join(
        os.path.join("C:\\", "Users", "user"),
        "AppData",
        "Roaming",
        "VanillaWoWLauncher",
    )


def test_config_dir_linux_uses_hidden_home_dir(
    fake_platform, monkeypatch, tmp_path
):
    fake_platform("linux")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert config_dir() == str(tmp_path / "home" / ".vanilla-wow-launcher")


def test_config_dir_linux_ignores_xdg(fake_platform, monkeypatch, tmp_path):
    fake_platform("linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert config_dir() == str(tmp_path / "home" / ".vanilla-wow-launcher")


def test_config_dir_macos_application_support(fake_platform, monkeypatch):
    fake_platform("darwin")
    monkeypatch.setenv("HOME", "/Users/user")
    assert (
        config_dir()
        == "/Users/user/Library/Application Support/VanillaWoWLauncher"
    )


def test_cache_dir_windows_uses_localappdata(
    fake_platform, monkeypatch, tmp_path
):
    fake_platform("win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert cache_dir() == str(tmp_path / "local" / "VanillaWoWLauncher")


def test_cache_dir_windows_falls_back_to_appdata(
    fake_platform, monkeypatch, tmp_path
):
    fake_platform("win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    assert cache_dir() == str(tmp_path / "roaming" / "VanillaWoWLauncher")


def test_cache_dir_linux_uses_xdg(fake_platform, monkeypatch, tmp_path):
    fake_platform("linux")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xcache"))
    assert cache_dir() == str(tmp_path / "xcache" / "vanilla-wow-launcher")


def test_cache_dir_macos(fake_platform, monkeypatch):
    fake_platform("darwin")
    monkeypatch.setenv("HOME", "/Users/user")
    assert cache_dir() == "/Users/user/Library/Caches/VanillaWoWLauncher"


def test_default_out_dir_non_windows_writable(fake_platform, monkeypatch):
    fake_platform("linux")
    monkeypatch.setenv("HOME", "/home/user")
    assert default_out_dir() == "/home/user/VanillaWoW"


# ── data dir ────────────────────────────────────────────────────────────────


def test_data_dir_linux_uses_xdg_data_home(
    fake_platform, monkeypatch, tmp_path
):
    fake_platform("linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdata"))
    assert data_dir() == str(tmp_path / "xdata" / "vanilla-wow-launcher")


def test_data_dir_linux_falls_back_to_local_share(
    fake_platform, monkeypatch, tmp_path
):
    fake_platform("linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert data_dir() == str(
        tmp_path / "home" / ".local" / "share" / "vanilla-wow-launcher"
    )


def test_data_dir_windows_uses_localappdata(
    fake_platform, monkeypatch, tmp_path
):
    fake_platform("win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert data_dir() == str(tmp_path / "local" / "VanillaWoWLauncher")


def test_data_dir_macos_application_support(fake_platform, monkeypatch):
    fake_platform("darwin")
    monkeypatch.setenv("HOME", "/Users/user")
    assert (
        data_dir()
        == "/Users/user/Library/Application Support/VanillaWoWLauncher"
    )


# ── open_folder ─────────────────────────────────────────────────────────────


def test_open_folder_windows(fake_platform, monkeypatch):
    fake_platform("win32")
    popen = mock.Mock()
    monkeypatch.setattr(subprocess, "Popen", popen)
    open_folder("C:\\games")
    popen.assert_called_once_with(
        ["explorer.exe", "C:\\games"], close_fds=True
    )


def test_open_folder_macos(fake_platform, monkeypatch):
    fake_platform("darwin")
    popen = mock.Mock()
    monkeypatch.setattr(subprocess, "Popen", popen)
    open_folder("/games")
    popen.assert_called_once_with(["open", "/games"], close_fds=True)


def test_open_folder_linux(fake_platform, monkeypatch):
    fake_platform("linux")
    popen = mock.Mock()
    monkeypatch.setattr(subprocess, "Popen", popen)
    open_folder("/games")
    popen.assert_called_once_with(["xdg-open", "/games"], close_fds=True)


def test_open_folder_missing_binary_raises(fake_platform, monkeypatch):
    fake_platform("linux")
    monkeypatch.setattr(
        subprocess,
        "Popen",
        mock.Mock(side_effect=FileNotFoundError("xdg-open")),
    )
    with pytest.raises(OSError):
        open_folder("/games")
