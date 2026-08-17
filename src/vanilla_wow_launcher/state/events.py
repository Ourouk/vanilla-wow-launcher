"""Thread-safe UI event channel.

A small, toolkit-agnostic event bus that worker threads use to talk to the
interface. Standard library only — no GUI toolkit. The UI thread drains
events once per event-loop tick and forwards them to the registered
handlers; `qt_bridge.ControllerBridge` converts them into Qt signals.
"""

import queue
import threading
from dataclasses import dataclass, field


class Event:
    """Base class for every event the dispatcher carries."""


@dataclass
class StatusChanged(Event):
    """The footer status line shown in the main window."""

    text: str


@dataclass
class LogMessage(Event):
    """One session-log line (text, tag)."""

    text: str
    tag: str = ""


@dataclass
class ProgressChanged(Event):
    """Progress-bar value in 0..1 plus the label shown above it."""

    value: float
    label: str = ""
    phase: str = ""
    transport: str = ""
    current_file: str = ""
    downloaded: int = 0
    total: int = 0
    speed: float = 0.0
    peers: int = 0
    verified_pieces: int = 0
    total_pieces: int = 0


@dataclass
class NewsLoaded(Event):
    """News-feed snapshot. kind is "featured" or "items"."""

    kind: str
    data: object | None = None


@dataclass
class ModsLoaded(Event):
    """MODS panel snapshot (ui_state.ModsState)."""

    state: object | None = None


@dataclass
class AddonsLoaded(Event):
    """ADDONS panel snapshot (ui_state.AddonsState)."""

    state: object | None = None


@dataclass
class MirrorStatusChanged(Event):
    """Download-mirror reachability result (Settings modal label)."""

    ok: bool
    text: str


@dataclass
class OperationFinished(Event):
    """A worker operation completed successfully."""

    kind: str
    ok: bool
    message: str = ""


@dataclass
class OperationFailed(Event):
    """A worker operation raised before it could report success."""

    kind: str
    message: str = ""


@dataclass
class UpdateFilesList(Event):
    """List of files identified as updated (from diff tree or torrent stale set)."""

    files: list[str] = field(default_factory=list)


@dataclass
class GameLaunched(Event):
    """A game process was started by the launcher (umu on Linux)."""

    pid: int
    pgid: int


@dataclass
class GameExited(Event):
    """A game process launched by the launcher has ended."""

    pid: int
    exit_code: int | None = None


class EventDispatcher:
    """Thread-safe, non-blocking event bus.

    Workers call post() from any thread; the UI thread calls drain() (or
    dispatch_all()) once per event-loop tick. A single lock guards both the
    queue and the handler list, so concurrent post/drain/subscribe calls can
    never lose an event or corrupt the handler set.
    """

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._handlers: list = []
        self._lock = threading.Lock()

    def post(self, event: Event) -> None:
        """Enqueue an event. Never blocks; safe from any thread."""
        self._queue.put_nowait(event)

    def drain(self) -> list:
        """Return every pending event, in post order, without blocking."""
        with self._lock:
            events = []
            while True:
                try:
                    events.append(self._queue.get_nowait())
                except queue.Empty:
                    break
        return events

    def subscribe(self, handler) -> None:
        """Register a handler to receive events via dispatch_all().
        Duplicate registrations of the same handler are ignored."""
        with self._lock:
            if handler not in self._handlers:
                self._handlers.append(handler)

    def unsubscribe(self, handler) -> None:
        """Remove a handler; an unknown handler is a no-op."""
        with self._lock:
            if handler in self._handlers:
                self._handlers.remove(handler)

    def dispatch_all(self, handler=None) -> list:
        """Drain the pending events and deliver each to `handler`, or to
        every subscribed handler when omitted. Events posted during delivery
        stay queued for the next call. Returns the dispatched events."""
        events = self.drain()
        if not events:
            return events
        if handler is not None:
            for event in events:
                handler(event)
        else:
            with self._lock:
                handlers = list(self._handlers)
            for event in events:
                for h in handlers:
                    h(event)
        return events

    def __len__(self) -> int:
        return self._queue.qsize()
