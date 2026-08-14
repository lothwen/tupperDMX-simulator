# ABOUTME: StageWindow — the top-level window the virtual stage lives in while
# ABOUTME: undocked, so it can be sized freely or moved to a second display.

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QVBoxLayout, QWidget


class StageWindow(QWidget):
    """A plain frame around the reparented StageView.

    Deliberately an ordinary window rather than a dialog or a tool window: it
    needs the system title bar so the user can drag it to another monitor,
    maximize it, and have it appear in the task switcher on its own.

    It never owns the view beyond its layout — MainWindow reparents the view
    back out before this window is destroyed.
    """

    dock_requested = pyqtSignal()

    def __init__(self, parent=None):
        # A parent would make this window always-on-top of the main window on
        # some platforms; the stage is meant to sit on its own screen.
        super().__init__(None)
        self.setWindowTitle("Virtual Stage")
        self.setMinimumSize(480, 320)
        self._closing_to_dock = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

    def close_silently(self) -> None:
        """Close without asking to be docked — the docking already happened."""
        self._closing_to_dock = True
        self.close()

    def closeEvent(self, event):
        # Closing the window is the other way of saying "put it back": the
        # stage must never be closable into nowhere.
        if not self._closing_to_dock:
            self.dock_requested.emit()
        super().closeEvent(event)
