"""Vanilla WoW Launcher Qt (PySide6) application context and shell.

Owns the process-wide QApplication singleton (`create_qt_app`) and the
`QtVanillaWoWLauncherApp` shell that the entry point imports for
`VANILLA_WOW_UI_BACKEND=qt`. The window chrome (header, tabs, footer) lives in
`qt_main_window.MainWindow`, the panels/dialogs in their qt_* modules, and
the business logic in the toolkit-agnostic controllers.
"""

import os
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase, QGuiApplication
from PySide6.QtWidgets import QApplication

from . import metrics as ui_metrics
from .bridge import ControllerHub
from .main_window import MainWindow
from .metrics import initial_window_size


def _load_bundled_font():
    """Load the bundled STIX Two Math font so ⌕ and ⧉ glyphs render."""
    if getattr(sys, "frozen", False):
        font_dir = os.path.join(sys._MEIPASS, "fonts")
    else:
        font_dir = os.path.join(
            os.path.dirname(
                os.path.dirname(
                    os.path.dirname(
                        os.path.dirname(
                            os.path.dirname(os.path.abspath(__file__))
                        )
                    )
                )
            ),
            "packaging",
            "fonts",
        )
    font_path = os.path.join(font_dir, "STIXTwoMath-Regular.otf")
    if os.path.isfile(font_path):
        QFontDatabase.addApplicationFont(font_path)


def create_qt_app():
    """Return the process-wide QApplication, creating it exactly once.

    High-DPI settings are configured before the instance exists: Qt handles
    high-DPI scaling natively, so only the scale factor rounding policy is
    set (PassThrough, so fractional display scales are preserved). It is only
    touched when no instance exists yet — later calls reuse it unchanged.
    """
    app = QApplication.instance()
    if app is not None:
        return app
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication([])
    _load_bundled_font()
    return app


def _initial_size(app):
    """Logical window size: the design size, capped at ~90% of the screen."""
    screen = app.primaryScreen()
    if screen is None:
        return ui_metrics.BASE_W, ui_metrics.BASE_H
    geo = screen.availableGeometry()
    return initial_window_size(geo.width(), geo.height())


def _center(window, app):
    """Center `window` on the primary screen's available geometry."""
    screen = app.primaryScreen()
    if screen is None:
        return
    geo = screen.availableGeometry()
    x = geo.x() + (geo.width() - window.width()) // 2
    y = geo.y() + (geo.height() - window.height()) // 2
    window.move(max(x, geo.x()), max(y, geo.y()))


class QtVanillaWoWLauncherApp:
    """Qt application shell — wires the controller hub into the main window.

    Construction never opens a window or starts the event loop, so it works
    headlessly (e.g. `QT_QPA_PLATFORM=offscreen`) and under tests.
    """

    def __init__(self):
        self._app = create_qt_app()
        self._hub = ControllerHub()
        self._window = MainWindow(self._hub)
        w, h = _initial_size(self._app)
        self._window.resize(w, h)
        _center(self._window, self._app)
        # Background verify/news/mod/addon/self-update schedule — fired by
        # the event loop, cancelled on window close. Kept out of
        # MainWindow.__init__ so the window stays side-effect-free when
        # constructed headlessly in tests.
        self._window.schedule_startup_tasks()

    def show(self):
        self._window.show()

    def run(self):
        """Start the event loop, showing the window if it isn't visible yet
        (callers should call `show()` first, but this makes the loop safe
        even if they forget)."""
        if not self._window.isVisible():
            self._window.show()
        return self._app.exec()

    def mainloop(self):
        """Alias for run(); the vanilla_wow_launcher entry point calls mainloop()."""
        return self.run()

    def close(self):
        self._window.close()
