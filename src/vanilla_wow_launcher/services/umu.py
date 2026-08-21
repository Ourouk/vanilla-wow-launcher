"""umu-launcher integration: run the Windows client via Proton/Wine on Linux.

umu (https://github.com/Open-Wine-Components/umu-launcher) is the Unified
Launcher for Windows Games on Linux. When ``umu-run`` is on PATH this launcher
can launch ``WoW.exe`` through Proton the same way a Steam game runs — no
Steam client or manual Wine prefix setup required.

Only Linux uses this module; the ``can_launch_client()`` capability in
`core/platform_support` enables the PLAY button when ``umu_available()`` is
true. Everything here is stdlib-only (umu itself does the Proton download,
prefix creation and protonfixes lookup; this module just locates the binary,
builds the env-var contract and spawns it detached).
"""

import os
import re
import shutil
import signal
import subprocess
import time

from ..core import platform_support
from ..core.log_sink import log

# A valid umu codename: umu-launcher uses UMU-Proton (its own, continuously
# updated Proton build) unless the user picks a specific installed build.
DEFAULT_PROTON = "UMU-Proton"
# Canonical codenames surfaced in the Proton selector alongside any locally
# installed builds discovered in the Steam compatibility-tools dirs.
PROTON_CODENAMES = ("UMU-Proton", "GE-Proton", "Proton-Experimental", "Proton")
# Not a real umu-database id — there is no Vanilla WoW entry — so it just
# names the prefix/token and skips unrelated game fixes.
DEFAULT_GAME_ID = "umu-vanilla-wow"
DEFAULT_STORE = "none"

# Renderer presets for the Vanilla-era (D3D8/OpenGL) client. Each maps to
# Proton env vars set on the umu-run process and to a Config.wtf gxApi value
# (written by services/tweaks.py). "auto" leaves Proton's defaults untouched.
RENDERER_AUTO = "auto"
RENDERER_DXVK_D3D8 = "dxvk-d3d8"
RENDERER_WINED3D_OPENGL = "wined3d-opengl"
RENDERER_CHOICES = (
    (RENDERER_AUTO, "Auto (Proton default)"),
    (RENDERER_DXVK_D3D8, "DXVK (D3D8)"),
    (RENDERER_WINED3D_OPENGL, "WineD3D (OpenGL)"),
)
DEFAULT_RENDERER = RENDERER_AUTO

_UMU_EXE = "umu-run"

# The compatibility-tools dirs Steam (native and Flatpak) reads for custom
# Proton builds. GE-Proton/UMU-Proton installs land here as subdirectories.
_COMPAT_TOOLS_DIRS = (
    "~/.local/share/Steam/compatibilitytools.d",
    "~/.var/app/com.valvesoftware.Steam/.local/share/Steam/compatibilitytools.d",
)
_UMU_PATH = "~/.local/bin/umu-run"


def find_umu() -> str:
    """Locate ``umu-run`` on PATH, falling back to ``~/.local/bin/umu-run``
    (the pip ``--user``/uv install location). Returns '' when missing."""
    on_path = shutil.which(_UMU_EXE)
    if on_path:
        return on_path
    fallback = os.path.expanduser(_UMU_PATH)
    if os.path.isfile(fallback) and os.access(fallback, os.X_OK):
        return fallback
    return ""


def umu_available() -> bool:
    """Whether umu-run is callable (cheap PATH probe)."""
    return bool(find_umu())


def find_gamemoderun() -> str:
    """Locate the Feral GameMode wrapper ``gamemoderun`` on PATH. Returns ''
    when GameMode isn't installed (the client can still launch without it)."""
    return shutil.which("gamemoderun") or ""


def is_wayland_session() -> bool:
    """Whether the current desktop session is Wayland (so the Proton/Wine
    Wayland backend is applicable)."""
    return os.environ.get("XDG_SESSION_TYPE") == "wayland" or bool(
        os.environ.get("WAYLAND_DISPLAY")
    )


def scan_linux_features() -> dict:
    """Probe optional Linux gaming features the launcher can toggle. Cheap,
    PATH/env based — safe to call when building the settings UI."""
    return {
        "gamemode_available": bool(find_gamemoderun()),
        "wayland_session": is_wayland_session(),
    }


def _version_key(name: str) -> tuple:
    """Sort key for Proton directory names: the numeric suffix tuple
    (e.g. GE-Proton9-4 → (9, 4)), else the raw string."""
    digits = re.findall(r"\d+", name)
    if digits:
        return tuple(int(d) for d in digits)
    return (0, name)


def resolve_proton(name: str) -> str:
    """Resolve a Proton *name* to a concrete path.

    Concrete paths (`~/GE-Proton9-4`, absolute paths) pass through unchanged.
    Codenames (`GE-Proton`, `UMU-Proton`) are resolved to the highest-numbered
    matching directory in the Steam compatibility-tools dirs. When nothing
    matches, the codename is passed through so umu can fall back to its own
    default (downloading UMU-Proton) or fail with its own message.
    """
    name = (name or "").strip()
    if not name or "/" in name or name.startswith("~"):
        return name
    for base in _COMPAT_TOOLS_DIRS:
        root = os.path.expanduser(base)
        if not os.path.isdir(root):
            continue
        exact = os.path.join(root, name)
        if os.path.isdir(exact):
            return exact
        candidates = [
            d
            for d in os.listdir(root)
            if d.startswith(name) and os.path.isdir(os.path.join(root, d))
        ]
        if candidates:
            best = max(candidates, key=_version_key)
            log(f"Proton {name} → {best}", "dim")
            return os.path.join(root, best)
    return name


def list_protons() -> list:
    """All Proton builds found in the Steam compatibility-tools dirs, newest
    first (by `_version_key`). Concrete directories only — the canonical
    codenames are added separately by `default_proton`/`available_protons`."""
    found = []
    for base in _COMPAT_TOOLS_DIRS:
        root = os.path.expanduser(base)
        if not os.path.isdir(root):
            continue
        for d in os.listdir(root):
            full = os.path.join(root, d)
            if os.path.isdir(full):
                found.append(d)
    # De-duplicate (a build could appear in more than one compat dir) then
    # sort newest-first.
    return sorted(set(found), key=_version_key, reverse=True)


def default_proton() -> str:
    """The Proton to use when the user hasn't pinned one: the newest locally
    installed build, else the `UMU-Proton` codename (which umu resolves to its
    own latest download)."""
    available = list_protons()
    if available:
        return available[0]
    return DEFAULT_PROTON


def available_protons() -> list:
    """The Proton selector contents: any installed builds (newest first)
    followed by the canonical codenames, de-duplicated and in a stable order
    with the default (`UMU-Proton`) first when present."""
    builds = list_protons()
    seen = set(builds)
    ordered = list(builds)
    for codename in PROTON_CODENAMES:
        if codename not in seen:
            ordered.append(codename)
            seen.add(codename)
    return ordered


def compute_wine_prefix() -> str:
    """The launcher-wide WINEPREFIX directory, created on demand.

    One prefix owned by the launcher (not per game folder), under the
    per-user data dir — the same prefix serves every game folder the launcher
    manages.
    """
    prefix = os.path.join(platform_support.data_dir(), "wineprefix")
    os.makedirs(prefix, exist_ok=True)
    return prefix


def build_env(
    proton: str,
    game_id: str,
    store: str = DEFAULT_STORE,
    renderer: str = DEFAULT_RENDERER,
    wayland: bool = False,
) -> dict:
    """The env-var contract umu expects, layered over the current process env
    (WINEPREFIX, PROTONPATH, GAMEID, STORE) plus renderer/backend-specific
    Proton flags. `renderer` is one of `RENDERER_*`; only "auto" leaves
    Proton's renderer defaults untouched. `wayland` enables the Proton/Wine
    Wayland backend (a no-op on X11)."""
    env = dict(os.environ)
    env["WINEPREFIX"] = compute_wine_prefix()
    env["PROTONPATH"] = resolve_proton(proton)
    env["GAMEID"] = game_id
    env["STORE"] = store
    if renderer == RENDERER_DXVK_D3D8:
        env["PROTON_DXVK_D3D8"] = "1"
    elif renderer == RENDERER_WINED3D_OPENGL:
        env["PROTON_USE_WINED3D"] = "1"
    if wayland:
        env["PROTON_ENABLE_WAYLAND"] = "1"
    return env


def launch(
    out_dir: str,
    exe: str,
    *,
    proton: str = DEFAULT_PROTON,
    game_id: str = DEFAULT_GAME_ID,
    store: str = DEFAULT_STORE,
    umu_binary: str = "",
    renderer: str = DEFAULT_RENDERER,
    gamemode: bool = False,
    wayland: bool = False,
) -> tuple:
    """Launch `exe` (an absolute path to the client binary) via umu-run.

    Spawns umu detached (its own session, no controlling terminal) with the
    env contract set (including renderer/backend Proton flags), cwd set to
    `out_dir`. When `gamemode` is set and GameMode is installed the wrapper is
    prepended so the client runs under Feral GameMode. Returns
    ``(pid, pgid, proc)`` — the umu-run PID, its POSIX process-group id (for
    killing the whole tree), and the Popen handle (so the caller can wait()
    on it). Raises when umu-run is missing or the spawn fails.
    """
    binary = umu_binary or find_umu()
    if not binary:
        raise RuntimeError(
            "umu-run not found — install umu-launcher (e.g. `pacman -S "
            "umu-launcher` or `apt install umu-launcher`) to play on Linux."
        )
    exe = os.path.abspath(exe)
    args = [binary, exe]
    if gamemode:
        gamemoderun = find_gamemoderun()
        if gamemoderun:
            args = [gamemoderun] + args
    proc = subprocess.Popen(
        args,
        cwd=out_dir,
        env=build_env(proton, game_id, store, renderer, wayland=wayland),
        start_new_session=True,
        close_fds=True,
    )
    try:
        pgid = os.getpgid(proc.pid)
    except (AttributeError, OSError):
        # Non-POSIX (Windows) or the process already exited — fall back to
        # the pid so a terminate still targets the process itself.
        pgid = proc.pid
    return proc.pid, pgid, proc


def kill_game(pid: int, pgid: int, grace: float = 2.0) -> None:
    """Terminate the game and its umu-run wrapper.

    On POSIX sends SIGTERM to the whole process group (umu + WoW.exe under
    Proton), then SIGKILL after `grace` seconds if it is still alive. A
    process that has already exited is a no-op.
    """
    if not platform_support.is_linux():
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
