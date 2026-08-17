"""Vanilla WoW Launcher — command-line entry point.

Wires the launcher configuration and the config-store paths, then starts the
Qt interface. The console-script entry (`vanilla-wow-launcher`), `python -m
vanilla_wow_launcher` and the PyInstaller specs all reach this module.

On first launch, when no `vanilla_wow_launcher.json` is found and no
``--launcher-config`` was given, a modal Qt wizard asks the user to pick one
instead of failing hard.
"""

import argparse
import os
import sys

from .core import config_store, launcher, platform_support
from .core.constants import (
    CACHE_FILE,
    CONFIG_FILE,
    LEGACY_CACHE_FILES,
    LEGACY_CONFIG_FILES,
    legacy_custom_pairs,
)

_QT_UNAVAILABLE = (
    "Vanilla WoW Launcher needs PySide6 (Qt) to run. "
    "Install it with `uv sync` or `pip install PySide6`.\n"
)


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="vanilla-wow-launcher",
        description="Vanilla WoW Launcher — updater and mod manager for the Vanilla "
        "WoW client.",
    )
    parser.add_argument(
        "--launcher-config",
        metavar="PATH",
        help="Path to the vanilla_wow_launcher.json file that configures "
        "the server, endpoints and mirrors (auto-discovered next to "
        "the executable / in the repo root when omitted).",
    )
    return parser.parse_args(argv)


def resolve_backend(name=None) -> type | None:
    """Return the Qt app class for the selected GUI backend.

    Reads the VANILLA_WOW_UI_BACKEND environment variable when ``name`` is None
    (``qt`` is the default; ``pyside6`` is accepted as an alias). Raises
    ImportError when the Qt module cannot be imported; returns None for an
    unknown backend name.
    """
    if name is None:
        name = os.environ.get("VANILLA_WOW_UI_BACKEND", "qt")
    if name in ("qt", "pyside6"):
        from .ui.qt.app import QtVanillaWoWLauncherApp

        return QtVanillaWoWLauncherApp
    return None


def backend_error_message(name, exc) -> str:
    """Map a failed backend import to a user-facing stderr message."""
    if name in ("qt", "pyside6"):
        return _QT_UNAVAILABLE
    return f"Failed to import the Vanilla WoW Launcher GUI: {exc}\n"


def main(argv=None) -> int:
    args = _parse_args(argv)
    explicit = bool(args.launcher_config)
    _cfg, err = launcher.configure(args.launcher_config)
    if err:
        if explicit:
            sys.stderr.write(f"{err}\n")
            return 1
        return _first_launch()
    return _run_backend()


def _first_launch() -> int:
    """No launcher config and no --launcher-config: ask the user to pick a
    server (or a local config file), then persist it so future launches reuse
    it. On first run with no game folder set, the client default is
    ~/Games/<ServerName>."""
    try:
        chosen = _pick_launcher_config()
    except ImportError as e:
        sys.stderr.write(backend_error_message("qt", e))
        return 1
    if chosen is None:
        sys.stderr.write(
            "No launcher configuration selected. A vanilla_wow_launcher.json "
            "is required — launch again with --launcher-config.\n"
        )
        return 1
    if chosen["kind"] == "file":
        _cfg, err = launcher.configure(chosen["path"])
        if err:
            sys.stderr.write(f"{err}\n")
            return 1
        dest, err = launcher.persist(chosen["path"])
        if err:
            sys.stderr.write(f"{err}\n")
            return 1
        if os.path.normpath(dest) != os.path.normpath(chosen["path"]):
            _cfg, err = launcher.configure(dest)
            if err:
                sys.stderr.write(f"{err}\n")
                return 1
    else:  # remote: config fetched from the repo
        import json

        from .services import server_index

        # Reuse the config the wizard already fetched when possible, otherwise
        # fetch it now (e.g. if the selection came from a non-interactive path).
        raw = chosen.get("raw")
        if not raw:
            _data, raw, err = server_index.fetch_server_config(
                chosen["config_url"]
            )
            if err:
                sys.stderr.write(f"{err}\n")
                return 1
        _cfg = launcher.configure_from_dict(json.loads(raw))
        if _cfg is None:
            sys.stderr.write(
                f"Invalid launcher configuration: {launcher.config_error()}\n"
            )
            return 1
        dest, err = launcher.persist_text(raw)
        if err:
            sys.stderr.write(f"{err}\n")
            return 1
        _cfg, err = launcher.configure(dest)
        if err:
            sys.stderr.write(f"{err}\n")
            return 1
    _ensure_default_game_folder()
    return _run_backend()


def _pick_launcher_config() -> dict | None:
    """Modal first-launch config picker; returns the chosen selection dict
    (``{"kind": "file", "path"}`` or ``{"kind": "remote", "config_url",
    "name"}``) or None on cancel."""
    from PySide6.QtWidgets import QDialog

    from .services import server_index
    from .ui.qt.app import create_qt_app
    from .ui.qt.launcher_config_dialog import LauncherConfigDialog

    # The index is fetched here (not inside the dialog) so the wizard never
    # blocks on the network while it is on screen; an unreadable index simply
    # yields an empty list and the dialog falls back to file browsing.
    servers = server_index.fetch_servers_index()
    create_qt_app()
    dlg = LauncherConfigDialog(
        servers=servers, initial_path=launcher.discover_path()
    )
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    return dlg.selection()


def _ensure_default_game_folder():
    """On first run with no game folder configured, default it to
    ~/Games/<ServerName> so the client installs there; the user can still
    change it in Settings."""
    cfg = config_store.load_config()
    if cfg.get("out_dir"):
        return
    name = launcher.server_name() or "VanillaWoW"
    folder = platform_support.server_games_dir(name)
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        return
    config_store.update_config(lambda c: c.__setitem__("out_dir", folder))


def _run_backend() -> int:
    """Config-store setup + Qt backend resolution/construction/run."""
    config_store.configure(
        CONFIG_FILE,
        CACHE_FILE,
        legacy_config=LEGACY_CONFIG_FILES,
        legacy_cache=LEGACY_CACHE_FILES,
        legacy_pairs=legacy_custom_pairs(),
    )
    backend = os.environ.get("VANILLA_WOW_UI_BACKEND", "qt")
    try:
        app_cls = resolve_backend(backend)
    except ImportError as e:
        sys.stderr.write(backend_error_message(backend, e))
        return 1
    if app_cls is None:
        sys.stderr.write(f"Unknown VANILLA_WOW_UI_BACKEND: {backend}\n")
        return 1
    try:
        app = app_cls()
    except Exception as e:
        sys.stderr.write(
            f"Vanilla WoW Launcher could not start: {e}\n"
            "A graphical display (X11/Wayland) is required.\n"
        )
        return 1
    app.show()
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
