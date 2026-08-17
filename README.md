# Vanilla WoW Launcher

Vanilla WoW Launcher is a desktop companion for Vanilla WoW clients. It updates
your game files, installs mods and addons, applies common graphics, camera,
sound, and gameplay preferences, and shows your server's news — all pointed at
whichever server you choose, with no built-in server list.

![Vanilla WoW Launcher](screenshot.png)

> For developers and server operators, see the
> [developer guide](docs/DEVELOPER.md).

## What It Does

- Updates the game client, downloading only the files that changed.
- Verifies downloaded files and resumes interrupted downloads.
- Bulk-downloads changed files over BitTorrent when your server offers it,
  falling back to plain HTTP automatically.
- Installs and updates registered client mods.
- Installs addons from supported Git hosting services without requiring Git.
- Applies common graphics, camera, sound, and gameplay preferences.
- Shows the configured server's news and featured announcements.
- Detects the installed client version.
- Supports mirrors when a server provides them.
- Keeps settings and installation records in your user profile.

The launcher does not include a game client, mods, addons, or patches. These
are provided by the server or content publishers configured for your launcher.

## Download

Download the latest release from:

<https://github.com/Ourouk/vanilla-wow-launcher/releases/latest>

Available packages may include:

| Platform | Package |
| --- | --- |
| Windows | `VanillaWoWLauncher-windows-x86_64.exe` |
| Linux | `VanillaWoWLauncher-linux-x86_64.AppImage` |
| macOS | `VanillaWoWLauncher-universal2.dmg` |

The release page also includes a matching `.sha256` checksum file for each
package and an example launcher configuration. Verify the checksum when
downloading from an untrusted or mirrored source.

## Installation

### Windows

1. Download `VanillaWoWLauncher-windows-x86_64.exe`.
2. Place it in a folder where you want to keep the launcher.
3. Run it and select the folder containing your Vanilla WoW client.
4. Follow the first-launch configuration wizard if prompted.

Windows may show a SmartScreen warning for an unsigned application. Only run
the executable if it came from a source you trust and its checksum matches the
release checksum.

### Linux

1. Download the AppImage.
2. Make it executable, for example with `chmod +x`.
3. Run the AppImage and select your game folder.

The AppImage includes the launcher's Qt libraries. Your desktop environment
must still provide a working graphical session, and game launching requires
[umu-launcher](https://github.com/Open-Wine-Components/umu-launcher)
(see [Linux](#linux)).

### macOS

1. Open the downloaded DMG.
2. Drag Vanilla WoW Launcher to Applications or another preferred location.
3. Open the launcher and select your game folder.

The macOS package supports both Apple Silicon and Intel Macs. It is unsigned
by default, so macOS may require confirmation in Privacy & Security before the
first launch. Game launching is not available on macOS.

## First Launch

The launcher uses a server configuration supplied by the server or
distribution you are using. It does not contain a hardcoded server list.

When no configuration is found, the first-launch wizard lets you select one.
The selected configuration is remembered for later launches. A configuration
can also be supplied explicitly when launching from a terminal:

```text
VanillaWoWLauncher --launcher-config PATH
```

Only use configuration files from a source you trust. A configuration controls
where the launcher retrieves game files, news, mods, addons, and update
metadata.

After selecting a configuration:

1. Open **Settings** and select the game folder if it was not detected.
2. Review the configured server and mirror information.
3. On first run, choose whether to install the server's essential mods and
   recommended addons when prompted.
4. Click **UPDATE** to run the complete update sequence: client, mods, then
   addons.
5. Use **PLAY** when the client is ready on supported platforms.

## Using the Launcher

### Client Updates

The launcher compares the files in your game folder with the configured server
manifest and downloads only the missing or changed files. Downloads can resume
after an interruption, and files are checked again after downloading.

When your server advertises a torrent, changed files are downloaded over
BitTorrent first and anything it missed is finished over HTTP automatically —
you just click **UPDATE**. If the manifest can't be reached but a torrent is
available, **UPDATE** offers a recovery re-download of the whole client. All of
this happens transparently; see the
[developer guide](docs/DEVELOPER.md#client-update-pipeline) for the details.

The **UPDATE** button runs updates in this order:

1. Client files are verified and updated.
2. Registered mods are installed or updated.
3. Addons are verified, then available addon updates are installed.

The next step starts only after the previous step completes successfully. If a
server does not provide a client manifest or client downloads, open **Settings**
and clear **Enable client updates**. This skips the client step and lets
**UPDATE** run the mods and addons steps. Client updates are enabled by
default.

Do not edit or remove files while an update or verification is running.

### Mods

The **MODS** tab lists the mods provided by the configured catalog. Essential
mods may be installed automatically when setting up a new game folder. Each
mod can be installed, updated, retried, or verified independently.

The available mods depend on the server configuration. There is no universal
built-in mod list.

### Addons

The **ADDONS** tab lists the addons provided by the configured catalog. Addons
can be installed or updated individually, or updated together. Custom Git
addons can be added from supported hosts, including GitHub, GitLab, Gitea, and
Codeberg, when the configured security policy allows the host.

Addon releases are pinned to a source revision where possible, which helps
ensure that an update is reproducible.

### Tweaks

The **TWEAKS** tab provides common settings such as:

- Field of view
- Render distance
- Nameplate range
- Camera distance
- Ground clutter distance
- Background sounds

Invalid values are rejected or limited to safe ranges. Use **Apply** to save
changes and **Reset** to restore the available defaults. Tweaks are written to
`Config.wtf`; runtime client fixes are left to the VanillaFixes loader mod
where installed, and the launcher never modifies `WoW.exe`.

### News

The **NEWS** tab displays announcements and featured posts from the configured
server.

### Discord

When your launcher configuration includes a Discord link, the header shows a
**DISCORD** button that opens your server's invitation or community page in
your web browser. The button is hidden when no link is configured.

### Settings

Settings lets you:

- Change the game folder.
- Check mirror availability.
- Verify game files.
- View session logs.
- Configure catalog URLs when the server permits it.
- Add the game folder to Windows Defender exclusions.
- Manage general launcher options.

General options include **Enable client updates**, which should be cleared when
the configured server does not publish client manifests or client downloads.

Only add a Defender exclusion for a game folder you control and trust.

### Linux

On Linux the **PLAY** button runs `WoW.exe` through
[umu-launcher](https://github.com/Open-Wine-Components/umu-launcher) (the
Unified Launcher for Windows Games), so no Steam client or manual Wine prefix
setup is required. To use it:

1. Install `umu-launcher` from your distribution (e.g. `pacman -S umu-launcher`
   or `apt install umu-launcher`) — `umu-run` must be on `PATH`.
2. Install a Proton build, e.g. a GE-Proton release under
   `~/.local/share/Steam/compatibilitytools.d` (a Steam install provides this),
   or let umu fetch the latest UMU-Proton.
3. Open **Settings → Linux (UMU)** to review the Proton path, the umu-run
   binary override, and the GAMEID token.

The launcher defaults to `GE-Proton` (umu resolves the newest installed
matching build) and uses a single launcher-wide Wine prefix. The **PLAY**
button only appears when `umu-run` is detected; otherwise the log suggests
installing it.

## Supported Platforms

| Capability | Windows | Linux | macOS |
| --- | :---: | :---: | :---: |
| Client file updates | Yes | Yes | Yes |
| Mods and addons | Yes | Yes | Yes |
| News and configuration | Yes | Yes | Yes |
| `Config.wtf` tweaks | Yes | Yes | Yes |
| Launch the Windows game client | Yes | Yes (via umu-launcher) | No |
| Windows Defender exclusions | Yes | No | No |

## Data Files

The launcher stores settings, installation records, and caches in your user
profile (Windows `%APPDATA%`, Linux `~/.vanilla-wow-launcher`, macOS
`~/Library/Application Support`). Deleting the settings directory resets the
launcher and starts first-time setup again. Delete these files only when the
launcher is closed and keep a backup if you need to preserve custom settings
or catalog entries.

## Security and Privacy

- Downloads use HTTPS with host restrictions derived from your configuration.
- Redirects remain HTTPS-only.
- Downloaded archives are extracted with protection against path traversal.
- Settings are stored in per-user directories rather than beside the
  executable.

The launcher retrieves content from the URLs and registries configured by you
or by the distribution that supplied the configuration. Review those URLs
before using the launcher. Do not provide credentials, private repositories, or
other sensitive information in a configuration file unless you understand how
the configured service handles it.

See the [developer guide](docs/DEVELOPER.md#security-model) for the full
security model.

## Troubleshooting

### The launcher asks for a configuration

This is expected when no launcher configuration is available. Ask the server
or distribution that provided the launcher for its configuration file, then
select it in the wizard.

### Updates fail or remain unavailable

Check that:

1. The game folder is correct and writable.
2. Your internet connection is working.
3. The server or mirror is online.
4. Your firewall or antivirus is not blocking the launcher.
5. The selected configuration came from a trusted, current source.

Use **Settings → Verify game files** and review the session log for more
details.

### Windows blocks the executable

Unsigned applications can trigger SmartScreen or antivirus warnings. Confirm
that the executable was downloaded from the official release page and compare
its SHA-256 checksum with the matching `.sha256` file before allowing it to run.

### macOS shows a security warning

The macOS package is unsigned by default. If you trust the download, use
macOS **System Settings → Privacy & Security** to allow the application after
the first blocked launch.

### Linux does not start the graphical interface

Make sure the AppImage is executable and that you are running it from an
active X11 or Wayland desktop session. The AppImage cannot provide a display
server.

## Legal Notices

World of Warcraft is a trademark of Blizzard Entertainment, Inc. Vanilla WoW
Launcher is **not affiliated with, endorsed by, or sponsored by Blizzard
Entertainment**.

The game client, mods, addons, patches, and other files handled by this tool
are created, hosted, and distributed by third parties. Vanilla WoW Launcher
does not create, host, own, or redistribute that content. It is a local
management tool and HTTP client that retrieves content from the URLs and
registries configured by the user.

You are responsible for ensuring that your use of the launcher and any
downloaded content complies with applicable laws, licenses, and the terms of
the relevant third parties. Check the license and distribution terms of every
client, mod, addon, patch, and service you use.

## Attribution and License

This project is derived from the original Octo Updater project:

- **rebasedkon** — original author
- Original project: <https://github.com/rebasedkon/octo-updater>

The derivative work is maintained by:

- **Andrea Spelgatti** — <spelgattiandrea@ourouk.be>

If you enjoy the launcher, you can support its development:

- Buy Me a Coffee: <https://buymeacoffee.com/ourouk>

The original author's attribution and donation links are retained as required
by the project license:

- Ko-fi: <https://ko-fi.com/rebased>
- Buy Me a Coffee: <https://buymeacoffee.com/rebased>
- Email: <inskon@proton.me>
- Discord: <https://discord.com/users/287467238573867018> (`rebazed`)

See the complete [LICENSE](LICENSE) for copyright, redistribution conditions,
trademark terms, warranty disclaimer, and contact information.
