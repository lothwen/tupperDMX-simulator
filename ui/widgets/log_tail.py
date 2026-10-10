# ABOUTME: LogTailView — a `tail -f` pane for one log file: polls for appended
# ABOUTME: bytes, colours the level column, and survives rotation.
"""Live view of a growing log file.

Deliberately a poll, not a QFileSystemWatcher: a file that is appended to from
several threads fires an event per flush, and the watcher would wake the GUI
thread hundreds of times a second for text nobody can read that fast. One timer
reading whatever arrived since last time costs a stat and a short read.

Only ever *reads* — pausing, clearing the view and switching files leave the
file alone; this pane cannot lose a line for whoever reads the file later.

Leaf widget: it is handed a path and knows nothing about who writes it. The
one thing it does know is the shape of a TupperDMX log line
(`time | logger | LEVEL | component | message`), and a line that doesn't match
is shown verbatim rather than dropped — a traceback is exactly that.
"""

import html
import os
from pathlib import Path

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QTextOption
from PyQt6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QPlainTextEdit,
                             QPushButton, QVBoxLayout, QWidget)

#: How often new bytes are picked up. Half a second reads as "live" and still
#: coalesces the burst a single frame of DMX logging produces.
POLL_MS = 500

#: How much of the file is read when the pane opens, and how many of the lines
#: in it are actually shown. The tail of a rotating log is the interesting end;
#: reading the whole 20 MB to throw most of it away is not.
OPEN_BYTES = 64 * 1024
OPEN_LINES = 300

#: Ceiling on the document. QPlainTextEdit drops blocks off the top for us, so
#: a pane left open all night costs the same as one just opened.
MAX_BLOCKS = 2000

#: Cap on one poll's worth of appending, so a subsystem that suddenly logs a
#: megabyte cannot freeze the GUI thread building HTML.
MAX_LINES_PER_POLL = 400

LEVEL_COLORS = {
    "DEBUG": "#7f8c94",
    "INFO": "#79cdf0",
    "WARNING": "#d9a13b",
    "ERROR": "#e05252",
    "CRITICAL": "#e07ce0",
}
TIME_COLOR = "#6f7780"
NAME_COLOR = "#a9b3bb"
TEXT_COLOR = "#d6d6d6"

#: Column widths, in characters, of the rendered line. The pane is monospace,
#: so padding with spaces is what aligns the columns.
TIME_WIDTH = 13
LEVEL_WIDTH = 9
NAME_WIDTH = 20


def _cell(text: str, width: int = 0) -> str:
    """One padded, HTML-escaped column. Runs of spaces become &nbsp; because
    rich text collapses them and the columns would close up.

    A value wider than its column is elided rather than allowed to push the
    message out of line: the full text is in the file, and what this pane is
    for is being readable at a glance.
    """
    if width and len(text) > width:
        text = text[:width - 2] + "…"
    return html.escape(text.ljust(width)).replace(" ", "&nbsp;")


def format_line(line: str) -> str:
    """One log line as the rich text the pane appends.

    A line the file formatter wrote splits into five fields; anything else
    (a traceback's continuation, a line from another writer) is passed through
    in the message colour rather than mangled into columns.
    """
    fields = line.split(" | ", 4)
    if len(fields) != 5:
        return f'<span style="color:{TEXT_COLOR}">{_cell(line)}</span>'

    stamp, name, level, _component, message = (f.strip() for f in fields)
    # The date is the file's, not the line's — every record in it is from the
    # same day, so only the clock earns a column.
    clock = stamp.split(" ", 1)[1] if " " in stamp else stamp
    color = LEVEL_COLORS.get(level, TEXT_COLOR)
    return (
        f'<span style="color:{TIME_COLOR}">{_cell(clock, TIME_WIDTH)}</span>'
        f'<span style="color:{color}">{_cell(level, LEVEL_WIDTH)}</span>'
        f'<span style="color:{NAME_COLOR}">{_cell(name, NAME_WIDTH)}</span>'
        f'<span style="color:{TEXT_COLOR}">{_cell(message)}</span>'
    )


def short_path(path) -> str:
    """`path` as folder/file — enough to recognise it, bounded in width so a
    deep folder cannot stretch the dialog that shows it."""
    parts = Path(path).parts
    return os.path.join(*parts[-2:]) if len(parts) > 1 else str(path)


class LogTailView(QWidget):
    """A status row (which file, follow, pause, clear) over a live log pane.

    Polling runs only while the widget is visible: this lives on one page of a
    QStackedWidget, and a settings dialog sitting on the Audio page has no
    business stat()ing a log file twice a second.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._path = None
        self._offset = 0        # how far into the file we have read
        self._inode = None      # rotation detector, where the OS supplies one
        self._partial = ""      # a last line that hasn't ended in \n yet
        self._missing_reported = False

        # The pane first: the header's Clear button connects straight to it.
        self.view = QPlainTextEdit()
        self.view.setObjectName("LogTailPane")
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(MAX_BLOCKS)
        self.view.setWordWrapMode(QTextOption.WrapMode.NoWrap)
        self.view.setPlaceholderText("Waiting for the log file…")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addLayout(self._build_header())
        layout.addWidget(self.view, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        self.status_label = QLabel()
        self.status_label.setObjectName("LogTailStatus")
        row.addWidget(self.status_label)
        row.addStretch(1)

        self.follow_check = QCheckBox("Follow tail")
        self.follow_check.setChecked(True)
        self.follow_check.setToolTip(
            "Keep the newest line in view as the log grows")
        self.follow_check.toggled.connect(self._on_follow_toggled)
        row.addWidget(self.follow_check)

        self.pause_button = QPushButton("Pause")
        self.pause_button.setCheckable(True)
        self.pause_button.setToolTip(
            "Stop reading new lines. The log itself keeps being written.")
        self.pause_button.toggled.connect(self._on_pause_toggled)
        row.addWidget(self.pause_button)

        self.clear_button = QPushButton("Clear view")
        self.clear_button.setToolTip(
            "Empty this pane. The log file is not touched.")
        self.clear_button.clicked.connect(self.view.clear)
        row.addWidget(self.clear_button)
        return row

    # --- what is being tailed ---------------------------------------------

    def set_path(self, path) -> None:
        """Tail `path` (or nothing, for None), from its tail end."""
        self._path = str(path) if path else None
        self._rewind()
        self._missing_reported = False
        self.view.clear()
        self._update_status()
        if self._path and self.isVisible() and not self.pause_button.isChecked():
            self._poll()

    def _rewind(self) -> None:
        """Forget where we were in the file — next poll opens it afresh.

        `_missing_reported` deliberately survives: a file that isn't there is
        rewound on every poll, and clearing the flag here would restyle the
        status label twice a second to say the same thing.
        """
        self._offset = 0
        self._inode = None
        self._partial = ""

    # --- polling ------------------------------------------------------------

    def showEvent(self, event):
        super().showEvent(event)
        if not self.pause_button.isChecked():
            self._poll()
            self._timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._timer.stop()

    def _on_pause_toggled(self, paused: bool) -> None:
        self.pause_button.setText("Resume" if paused else "Pause")
        if paused:
            self._timer.stop()
        elif self.isVisible():
            self._poll()
            self._timer.start()
        self._update_status()

    def _on_follow_toggled(self, follow: bool) -> None:
        if follow:
            self._scroll_to_end()

    def _poll(self) -> None:
        if not self._path:
            return
        try:
            stat = os.stat(self._path)
        except OSError:
            # Not created yet, or rotated out from under us between polls.
            # Either way the next one that finds a file starts it over.
            self._rewind()
            self._report_missing()
            return

        # A file that shrank, or whose inode changed, is a *different* file:
        # RotatingFileHandler renames ours away and opens a new one at zero.
        rotated = (stat.st_size < self._offset
                   or (self._inode not in (None, 0, stat.st_ino)))
        if rotated:
            self._rewind()

        first_read = self._inode is None
        self._inode = stat.st_ino
        if first_read:
            # Open on the tail end, not the top of a 20 MB file.
            seek_from = max(0, stat.st_size - OPEN_BYTES)
        elif stat.st_size == self._offset:
            return
        else:
            seek_from = self._offset

        try:
            with open(self._path, "r", encoding="utf-8", errors="replace") as handle:
                handle.seek(seek_from)
                chunk = handle.read()
                self._offset = handle.tell()
        except OSError:
            self._report_missing()
            return

        self._missing_reported = False
        self._append(chunk, first_read, seek_from)
        self._update_status()

    def _report_missing(self) -> None:
        if not self._missing_reported:
            self._missing_reported = True
            self._update_status()

    def _append(self, chunk: str, first_read: bool, seek_from: int) -> None:
        """Render whatever complete lines `chunk` completes."""
        text = self._partial + chunk
        lines = text.split("\n")
        self._partial = lines.pop()  # after the last \n; may still grow

        if first_read and seek_from > 0 and lines:
            lines.pop(0)  # we seeked into the middle of a line
        if first_read:
            lines = lines[-OPEN_LINES:]
        elif len(lines) > MAX_LINES_PER_POLL:
            skipped = len(lines) - MAX_LINES_PER_POLL
            lines = lines[-MAX_LINES_PER_POLL:]
            lines.insert(0, f"… {skipped} lines skipped")

        for line in lines:
            if line.strip():
                self.view.appendHtml(format_line(line.rstrip("\r")))
        if lines and self.follow_check.isChecked():
            self._scroll_to_end()

    def _scroll_to_end(self) -> None:
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())

    # --- status line --------------------------------------------------------

    def _update_status(self) -> None:
        if not self._path:
            self.status_label.setText(self._status("#8a8a8a", "no log file"))
        elif self.pause_button.isChecked():
            self.status_label.setText(
                self._status("#d9a13b", f"paused · {short_path(self._path)}"))
        elif self._missing_reported:
            self.status_label.setText(self._status(
                "#8a8a8a", f"waiting for {short_path(self._path)}"))
        else:
            self.status_label.setText(self._status(
                "#4CAF50", f"tailing {short_path(self._path)}"))

    @staticmethod
    def _status(color: str, text: str) -> str:
        return (f'<span style="color:{color}">●</span> '
                f'<span>{html.escape(text)}</span>')
