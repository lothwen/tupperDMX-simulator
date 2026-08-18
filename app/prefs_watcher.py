# ABOUTME: Watches userprefs.json for edits made outside the app — an operator
# ABOUTME: editing the rack box's prefs over SMB — and reports them, coalesced,
# ABOUTME: to AppCore. QtCore only; it knows nothing about what is in the file.
import os

from PyQt6.QtCore import QFileSystemWatcher, QObject, QTimer, pyqtSignal


class PrefsWatcher(QObject):
    """Emits `changed` a short moment after userprefs.json is touched.

    Deliberately dumb: it reports that the file moved, never what is in it.
    Whether the change was the app's own save() is a question about content,
    which only Config can answer (Config.reload_prefs).
    """

    changed = pyqtSignal()

    #: Coalescing window. One logical save is several events — an editor
    #: rewriting in place fires a change per write, and a save-by-rename fires
    #: one for the directory and one for the file.
    SETTLE_MS = 300

    #: A file caught mid-write parses as garbage. Look again a few times before
    #: giving up and waiting for the next event.
    MAX_RETRIES = 3

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self._path = str(path)
        self._retries = 0

        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._touched)
        self._watcher.directoryChanged.connect(self._touched)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.SETTLE_MS)
        self._timer.timeout.connect(self._settled)

        self._rearm()

    def _rearm(self) -> None:
        """(Re-)watch the file AND its directory.

        Both, because Qt drops a watched path whose file is replaced and every
        writer here replaces rather than rewrites: atomic_write_json() renames a
        temp file over it, and so does any editor that saves atomically. The
        directory watch is what still fires while the file is momentarily
        absent, and what catches its recreation.
        """
        if self._path not in self._watcher.files() and os.path.exists(self._path):
            self._watcher.addPath(self._path)
        parent = os.path.dirname(self._path)
        if (parent and os.path.isdir(parent)
                and parent not in self._watcher.directories()):
            self._watcher.addPath(parent)

    def _touched(self, _path=None) -> None:
        self._rearm()
        self._retries = 0
        self._timer.start()

    def _settled(self) -> None:
        self._rearm()
        self.changed.emit()

    def retry(self) -> bool:
        """Ask for another look — the file did not parse, most likely caught
        between an editor's truncate and its write. False once the retries are
        spent, which is the caller's cue to complain rather than stay quiet."""
        if self._retries >= self.MAX_RETRIES:
            return False
        self._retries += 1
        self._timer.start()
        return True

    def stop(self) -> None:
        self._timer.stop()
        paths = self._watcher.files() + self._watcher.directories()
        if paths:
            self._watcher.removePaths(paths)
