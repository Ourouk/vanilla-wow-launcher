"""Toolkit-agnostic shared application-state models.

Pure dataclasses that capture the runtime state of every panel — the
controllers mutate them and the Qt layer renders them. No GUI toolkit, no
threading. Field names match the on-disk config keys and the session records
the controllers keep.
"""

from dataclasses import dataclass, field

# ── update / verification flow ────────────────────────────────────────────────


@dataclass
class UpdateState:
    """Footer state: status text, progress, and the verify/update lifecycle
    flags (_status_var / _pb_val / _prog_label_var / _running /
    _client_ready / _diff_nodes / _client_ver_var)."""

    status: str = "Ready to update"
    progress: float = 0.0
    progress_label: str = ""
    progress_phase: str = ""
    progress_transport: str = ""
    progress_file: str = ""
    progress_downloaded: int = 0
    progress_total: int = 0
    progress_speed: float = 0.0
    progress_peers: int = 0
    progress_verified_pieces: int = 0
    progress_total_pieces: int = 0
    running: bool = False
    client_ready: bool = False
    manifest_available: bool = False
    diff_nodes: list | None = None
    torrent_stale: list | None = None
    torrent_reachable: bool | None = None
    torrent_error: str | None = None
    client_version: str = ""
    game_running: bool = False
    game_pid: int | None = None
    game_pgid: int | None = None


# ── news ──────────────────────────────────────────────────────────────────────


@dataclass
class NewsState:
    """Cached news feed (_featured, _news_items, _feat_ts, _news_ts)."""

    featured: dict | None = None
    items: list | None = None
    feat_ts: float = 0.0
    news_ts: float = 0.0


# ── mods ──────────────────────────────────────────────────────────────────────


@dataclass
class ModState:
    """One per-mod config record ("mods" key): enabled, installed_version,
    installed_files, ignore_updates, error.

    ``present`` is the session-computed filesystem-truth flag (files on disk +
    dlls.txt registration); it is not a config key.
    """

    enabled: bool = False
    installed_version: str | None = None
    installed_files: list = field(default_factory=list)
    ignore_updates: bool = False
    error: str | None = None
    present: bool = False

    @property
    def has_error(self) -> bool:
        return bool(self.error)


@dataclass
class ModPending:
    """One not-yet-applied checkbox change from _mod_pending_state."""

    enabled: bool | None = None
    ignore_updates: bool | None = None


@dataclass
class ModsState:
    """MODS panel state: config records, fetched latest versions
    (_mods_state), pending checkbox changes, the nav-badge count, and
    filesystem-detected mods no catalog claims (``unknown``)."""

    records: dict[str, ModState] = field(default_factory=dict)
    latest_versions: dict[str, str] = field(default_factory=dict)
    pending: dict[str, ModPending] = field(default_factory=dict)
    updates_count: int = 0
    unknown: list = field(default_factory=list)

    def latest_version(self, mod_id: str) -> str | None:
        return self.latest_versions.get(mod_id)

    @property
    def has_errors(self) -> bool:
        return any(rec.has_error for rec in self.records.values())

    @property
    def has_pending_changes(self) -> bool:
        return bool(self.pending)


# ── addons ────────────────────────────────────────────────────────────────────


@dataclass
class AddonState:
    """One addon record — the shape stored in the config's "addons" key
    ({"folder", "status", "git", "branch", "ref", "toc", "description",
    "error"})."""

    folder: str
    status: str = "available"
    git: str | None = None
    branch: str | None = None
    ref: str | None = None
    toc: dict = field(default_factory=dict)
    description: str | None = None
    error: str | None = None

    @classmethod
    def from_dict(cls, rec: dict) -> "AddonState":
        return cls(
            **{name: rec.get(name) for name in cls.__dataclass_fields__}
        )

    def to_dict(self) -> dict:
        return {
            name: getattr(self, name) for name in self.__dataclass_fields__
        }


@dataclass
class AddonError:
    """Session install/update failure carried across rescans
    (_addon_errors: {folder: {"error", "git"}})."""

    error: str
    git: str | None = None


@dataclass
class AddonsState:
    """ADDONS panel state, mirroring _addons_status, _addons_busy,
    _addons_installing, _addons_verified_ts, _addon_errors,
    _addon_sections_open and _addon_updates_count."""

    state: str = "idle"
    addons: dict[str, AddonState] = field(default_factory=dict)
    available: list[AddonState] = field(default_factory=list)
    busy: bool = False
    installing: bool = False
    verified_ts: float = 0.0
    errors: dict[str, AddonError] = field(default_factory=dict)
    sections_open: dict[str, bool] = field(
        default_factory=lambda: {"INSTALLED": True, "AVAILABLE": True}
    )
    updates_count: int = 0

    @classmethod
    def from_status_dict(cls, status: dict) -> "AddonsState":
        """Build from an app._addons_status dict ({state, addons, available})."""
        return cls(
            state=status.get("state", "idle"),
            addons={
                folder: AddonState.from_dict(rec)
                for folder, rec in status.get("addons", {}).items()
            },
            available=[
                AddonState.from_dict(rec)
                for rec in status.get("available", [])
            ],
        )

    def to_status_dict(self) -> dict:
        """The app._addons_status dict shape ({state, addons, available})."""
        return {
            "state": self.state,
            "addons": {
                folder: rec.to_dict() for folder, rec in self.addons.items()
            },
            "available": [rec.to_dict() for rec in self.available],
        }

    def out_of_date_count(self) -> int:
        return sum(
            1 for rec in self.addons.values() if rec.status == "outOfDate"
        )


# ── settings / path ───────────────────────────────────────────────────────────


@dataclass
class SettingsState:
    """Game-folder path (_path_var), the loaded config dict (_cfg) and the
    first-run flags."""

    path: str = ""
    config: dict = field(default_factory=dict)
    first_run: bool = False
    first_run_av_pending: bool = False
    first_run_verify_pending: bool = False


@dataclass
class LaunchSettings:
    """Linux umu-launcher launch settings (the "launch" config key)."""

    umu_proton: str = "GE-Proton"  # PROTONPATH value (codename or path)
    umu_binary_path: str = ""  # "" = auto-detect umu-run on PATH
    umu_game_id: str = "umu-vanilla-wow"

    @classmethod
    def from_config(cls, cfg: dict) -> "LaunchSettings":
        data = cfg.get("launch") or {}
        return cls(
            umu_proton=data.get("umu_proton", "GE-Proton"),
            umu_binary_path=data.get("umu_binary_path", ""),
            umu_game_id=data.get("umu_game_id", "umu-vanilla-wow"),
        )


# ── log ───────────────────────────────────────────────────────────────────────


@dataclass
class LogEntry:
    """One session-log line (the _log_buffer (text, tag) pairs)."""

    text: str
    tag: str = ""


# ── app container ─────────────────────────────────────────────────────────────


@dataclass
class AppState:
    """Everything the interface needs, in one object the controllers share."""

    update: UpdateState = field(default_factory=UpdateState)
    news: NewsState = field(default_factory=NewsState)
    mods: ModsState = field(default_factory=ModsState)
    addons: AddonsState = field(default_factory=AddonsState)
    settings: SettingsState = field(default_factory=SettingsState)
    log_buffer: list[LogEntry] = field(default_factory=list)

    def add_log(self, text: str, tag: str = "") -> None:
        self.log_buffer.append(LogEntry(text, tag))

    def log_lines(self) -> list[tuple[str, str]]:
        """The _log_buffer list of (text, tag) tuples for rendering."""
        return [(entry.text, entry.tag) for entry in self.log_buffer]
