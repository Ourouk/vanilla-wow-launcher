"""Qt controller bridge — dispatcher events → Qt signals.

`ControllerBridge` turns events posted on the toolkit-agnostic
`EventDispatcher` (by the controllers' worker threads) into Qt signals on the
Qt main thread. A QTimer polls the dispatcher every 50 ms and emits one
signal per event, so the UI connects to plain signals instead of polling the
bus itself. Workers never emit signals directly; the timer guarantees
delivery happens on the main thread.

Event → signal mapping:

    StatusChanged(text)                  → statusChanged(str)
    LogMessage(text, tag)                → logMessage(str, str)
    ProgressChanged(value, label)        → progressChanged(float, str)
    NewsLoaded(kind, data)               → newsLoaded(object)       # NewsLoaded
    ModsLoaded(state)                    → modsLoaded(object)       # ModsLoaded
    AddonsLoaded(state)                  → addonsLoaded(object)     # AddonsLoaded
    MirrorStatusChanged(ok, text)        → mirrorStatusChanged(bool, str)
    OperationFinished(kind, ok, message) → operationFinished(str, bool, str)
    OperationFailed(kind, message)       → operationFailed(str, str)
    GameLaunched(pid, pgid)              → gameLaunched(int, int)
    GameExited(pid, exit_code)           → gameExited(int, object)

The object-typed signals carry the full event dataclass (kind + payload); the
scalar signals carry the event's fields in argument order. `ControllerHub` is a
thin convenience that assembles the six controllers on one shared dispatcher
together with the bridge — the main window may equally do the assembly by hand.
"""

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from ...controllers.addons import AddonsController
from ...controllers.mods import ModsController
from ...controllers.news import NewsController
from ...controllers.settings import SettingsController
from ...controllers.tweaks import TweaksController
from ...controllers.update import UpdateController
from ...state.events import (
    AddonsLoaded,
    EventDispatcher,
    GameExited,
    GameLaunched,
    LogMessage,
    MirrorStatusChanged,
    ModsLoaded,
    NewsLoaded,
    OperationFailed,
    OperationFinished,
    ProgressChanged,
    StatusChanged,
    UpdateFilesList,
)

# Poll interval: responsive without busy-spinning the event loop.
_DRAIN_INTERVAL_MS = 50


class ControllerBridge(QObject):
    """Receives EventDispatcher events and re-emits them as Qt signals.

    The dispatcher is drained by a QTimer on the Qt main thread, so delivery
    order matches post order and no locking is needed beyond what the
    dispatcher provides. `close()`/`stop()` tears the bridge down.
    """

    statusChanged = Signal(str)
    logMessage = Signal(str, str)
    progressChanged = Signal(float, str)
    updateProgressChanged = Signal(object)
    updateFilesList = Signal(object)
    newsLoaded = Signal(object)
    modsLoaded = Signal(object)
    addonsLoaded = Signal(object)
    mirrorStatusChanged = Signal(bool, str)
    operationFinished = Signal(str, bool, str)
    operationFailed = Signal(str, str)
    gameLaunched = Signal(int, int)
    gameExited = Signal(int, object)

    def __init__(self, dispatcher: EventDispatcher, parent=None):
        super().__init__(parent)
        self._dispatcher = dispatcher
        self._closed = False
        self._subscription = self._on_event
        dispatcher.subscribe(self._subscription)
        self._timer = QTimer(self)
        self._timer.setInterval(_DRAIN_INTERVAL_MS)
        self._timer.timeout.connect(self._drain)
        self._timer.start()

    @Slot()
    def _drain(self):
        """Drain the pending events and emit the matching signal per event."""
        self._dispatcher.dispatch_all()

    def _on_event(self, event):
        if isinstance(event, StatusChanged):
            self.statusChanged.emit(event.text)
        elif isinstance(event, LogMessage):
            self.logMessage.emit(event.text, event.tag)
        elif isinstance(event, ProgressChanged):
            self.progressChanged.emit(event.value, event.label)
            self.updateProgressChanged.emit(event)
        elif isinstance(event, UpdateFilesList):
            self.updateFilesList.emit(event)
        elif isinstance(event, NewsLoaded):
            self.newsLoaded.emit(event)
        elif isinstance(event, ModsLoaded):
            self.modsLoaded.emit(event)
        elif isinstance(event, AddonsLoaded):
            self.addonsLoaded.emit(event)
        elif isinstance(event, MirrorStatusChanged):
            self.mirrorStatusChanged.emit(event.ok, event.text)
        elif isinstance(event, OperationFinished):
            self.operationFinished.emit(event.kind, event.ok, event.message)
        elif isinstance(event, OperationFailed):
            self.operationFailed.emit(event.kind, event.message)
        elif isinstance(event, GameLaunched):
            self.gameLaunched.emit(event.pid, event.pgid)
        elif isinstance(event, GameExited):
            self.gameExited.emit(event.pid, event.exit_code)

    def close(self):
        """Stop polling and unsubscribe from the dispatcher.

        Idempotent: repeated shutdown (e.g. the window close and test
        teardown both closing the hub) is a safe no-op."""
        if self._closed:
            return
        self._closed = True
        self._timer.stop()
        self._dispatcher.unsubscribe(self._subscription)

    def stop(self):
        """Alias for close()."""
        self.close()


class ControllerHub:
    """Assembles the six controllers on one shared dispatcher plus the bridge.

    Plain Python object (no QObject); it exists so the Qt main window gets a
    ready-made wiring in one line. The bridge shares the hub's dispatcher.
    """

    def __init__(self, get_out_dir=None):
        self.dispatcher = EventDispatcher()
        self.updater = UpdateController(self.dispatcher, get_out_dir)
        self.news = NewsController(self.dispatcher)
        self.mods = ModsController(self.dispatcher, get_out_dir)
        self.addons = AddonsController(self.dispatcher, get_out_dir)
        self.tweaks = TweaksController(self.dispatcher, get_out_dir)
        self.settings = SettingsController(
            self.dispatcher, self.updater, self.mods, self.addons, self.news
        )
        self.bridge = ControllerBridge(self.dispatcher)

    def close(self):
        self.bridge.close()
