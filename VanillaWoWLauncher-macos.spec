# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the macOS .app bundle — *universal2* (arm64 + x86_64).

Build on macOS with a universal-capable Python/PySide6 environment:

    uv sync --dev
    uv run pyinstaller --noconfirm --clean VanillaWoWLauncher-macos.spec

Produces ``dist/VanillaWoWLauncher.app``; ``packaging/macos/build-dmg.sh``
verifies both architectures with ``lipo`` and wraps it into a .dmg. The app
is unsigned by default; pass ``CODESIGN_IDENTITY`` to the build script for
ad-hoc/Developer-ID signing. UPX is disabled (not supported on macOS) and
``target_arch="universal2"`` requires every bundled binary (incl. the Qt
frameworks) to be multi-arch — a single-arch PySide6 install yields a
single-arch or failed build.
"""

from PyInstaller.utils.hooks import collect_all

pyside_datas, pyside_binaries, pyside_hiddenimports = collect_all("PySide6")
shiboken_datas, shiboken_binaries, shiboken_hiddenimports = collect_all("shiboken6")
lt_datas, lt_binaries, lt_hiddenimports = collect_all("libtorrent")
datas = pyside_datas + shiboken_datas + lt_datas
binaries = pyside_binaries + shiboken_binaries + lt_binaries
hiddenimports = pyside_hiddenimports + shiboken_hiddenimports + lt_hiddenimports

# The panels/dialogs are constructed by the Qt main window at runtime, so
# list every app module explicitly to be safe under a frozen build.
hiddenimports += [
    "vanilla_wow_launcher.core.constants",
    "vanilla_wow_launcher.core.config_store",
    "vanilla_wow_launcher.core.errors",
    "vanilla_wow_launcher.core.filesystem",
    "vanilla_wow_launcher.core.helpers",
    "vanilla_wow_launcher.core.log_sink",
    "vanilla_wow_launcher.core.platform_support",
    "vanilla_wow_launcher.core.security_http",
    "vanilla_wow_launcher.services.addons",
    "vanilla_wow_launcher.services.client_update",
    "vanilla_wow_launcher.services.mods",
    "vanilla_wow_launcher.services.news",
    "vanilla_wow_launcher.services.self_update",
    "vanilla_wow_launcher.services.torrent_download",
    "vanilla_wow_launcher.services.tweaks",
    "vanilla_wow_launcher.controllers.addons",
    "vanilla_wow_launcher.controllers.mods",
    "vanilla_wow_launcher.controllers.news",
    "vanilla_wow_launcher.controllers.settings",
    "vanilla_wow_launcher.controllers.tweaks",
    "vanilla_wow_launcher.controllers.update",
    "vanilla_wow_launcher.state.models",
    "vanilla_wow_launcher.state.events",
    "vanilla_wow_launcher.ui.qt.metrics",
    "vanilla_wow_launcher.ui.qt.addons_panel",
    "vanilla_wow_launcher.ui.qt.app",
    "vanilla_wow_launcher.ui.qt.bridge",
    "vanilla_wow_launcher.ui.qt.custom_addon_dialog",
    "vanilla_wow_launcher.ui.qt.log_window",
    "vanilla_wow_launcher.ui.qt.main_window",
    "vanilla_wow_launcher.ui.qt.mods_panel",
    "vanilla_wow_launcher.ui.qt.news_panel",
    "vanilla_wow_launcher.ui.qt.settings_dialog",
    "vanilla_wow_launcher.ui.qt.theme",
    "vanilla_wow_launcher.ui.qt.tweaks_panel",
]


a = Analysis(
    ["packaging/pyinstaller_entry.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VanillaWoWLauncher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="universal2",
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="VanillaWoWLauncher",
)

app = BUNDLE(
    coll,
    name="VanillaWoWLauncher.app",
    icon="packaging/macos/VanillaWoWLauncher.icns",
    bundle_identifier="org.vanillawow.launcher",
    version="1.2",
    info_plist={
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
        "LSMinimumSystemVersion": "11.0",
    },
)
