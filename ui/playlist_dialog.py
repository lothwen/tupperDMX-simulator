# ABOUTME: PlaylistDialog — the playlist manager: every song of the show in one
# ABOUTME: table (reorder, new/delete, filter, import, on/off, title, tracks,
# ABOUTME: BPM, key, precount), each edit applied the moment it is made.
import os

from PyQt6.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFontDatabase, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPushButton, QSizePolicy, QSpinBox, QTableWidget,
    QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from core.paths import AUDIO_SUFFIXES
from core.tempo_map import MAX_BPM, MIN_BPM
from ui import theme
from ui.import_songs_dialog import ImportSongsDialog
from ui.widgets import icons
from ui.widgets.sizing import fit_to_screen
from ui.widgets.toggle_switch import ToggleSwitch

# The key picker's list. The field stays editable, so a spelling a show
# already carries (or one typed in the toolbar) survives.
KEYS = ("", "C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B",
        "Cm", "C#m", "Dm", "Ebm", "Em", "Fm", "F#m", "Gm", "G#m", "Am", "Bbm",
        "Bm")

(COL_GRIP, COL_ON, COL_NUM, COL_NAME, COL_GUIDE, COL_BACKING, COL_BPM,
 COL_KEY, COL_PRECOUNT) = range(9)
HEADERS = ("", "ON", "#", "NAME", "GUIDE TRACK", "BACKING TRACK", "BPM", "KEY",
           "PRECOUNT")
# Column widths at ui_scale 1.0 (NAME takes what is left). Set in Python, so
# they are multiplied by the applied scale by hand — the QSS rewrite that
# scales everything else never sees them.
COLUMN_WIDTHS = {COL_GRIP: 30, COL_ON: 44, COL_NUM: 40, COL_GUIDE: 190,
                 COL_BACKING: 190, COL_BPM: 84, COL_KEY: 100, COL_PRECOUNT: 90}
ROW_HEIGHT = 44

# How long the BPM field waits after the last arrow step before it writes. A
# song that is not loaded has its .mid rescaled on every tempo write, and
# every rescale rounds its ticks — stepping 120 → 130 must be one write.
BPM_COMMIT_MS = 600

AUDIO_FILTER = ("Audio files (" + " ".join(f"*{s}" for s in AUDIO_SUFFIXES)
                + ");;All files (*)")

_SONG_ID = Qt.ItemDataRole.UserRole
_DIM = QColor("#6a6a6a")
# Rows are never drop targets: a drop lands BETWEEN rows (see dropEvent).
_ROW_FLAGS = (Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
              | Qt.ItemFlag.ItemIsDragEnabled)


def _placeholder(row):
    return f"Song {row + 1}"


class _SongTable(QTableWidget):
    """The songs, reorderable by drag. As in the fixture editor's chase list,
    a drop is reported as (from, to) and Qt never moves a row: the dialog
    rebuilds from the setlist, so the two cannot drift."""

    move_requested = pyqtSignal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        # Named BEFORE it has columns: laying out columns polishes the header,
        # and a header polished before its table had a name never matches the
        # `QTableWidget#PlaylistTable QHeaderView` rules.
        self.setObjectName("PlaylistTable")
        self.setColumnCount(len(HEADERS))
        self.setHorizontalHeaderLabels(HEADERS)
        self.verticalHeader().hide()
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                             | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDragEnabled(True)
        self.setDropIndicatorShown(True)

        scale = theme.applied_scale()
        header = self.horizontalHeader()
        header.setHighlightSections(False)
        header.setSectionsClickable(False)
        for col in range(len(HEADERS)):
            if col == COL_NAME:
                header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
            else:
                header.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
                self.setColumnWidth(col, round(COLUMN_WIDTHS[col] * scale))
            centered = col in (COL_ON, COL_PRECOUNT)
            self.horizontalHeaderItem(col).setTextAlignment(
                (Qt.AlignmentFlag.AlignCenter if centered
                 else Qt.AlignmentFlag.AlignLeft) | Qt.AlignmentFlag.AlignVCenter)
        rows = self.verticalHeader()
        rows.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        rows.setDefaultSectionSize(round(ROW_HEIGHT * scale))
        # Hover (no button held) reaches mouseMoveEvent only with tracking on.
        self.viewport().setMouseTracking(True)

    def setCellWidget(self, row, column, widget):
        # Cell widgets are the viewport's children: without a cursor of their
        # own they would inherit the grip's hand the moment the pointer crossed
        # from the grip into them (the viewport sees no move event then).
        widget.setCursor(Qt.CursorShape.ArrowCursor)
        super().setCellWidget(row, column, widget)

    def mouseMoveEvent(self, event):
        """An open hand over the grip dots — only while rows can be dragged
        (the filter switches reordering off)."""
        if not event.buttons():
            pos = event.position().toPoint()
            if (self.dragEnabled() and self.columnAt(pos.x()) == COL_GRIP
                    and self.rowAt(pos.y()) >= 0):
                self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            else:
                self.viewport().unsetCursor()
        super().mouseMoveEvent(event)

    def dropEvent(self, event):
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()
        source = self.currentRow()
        if source < 0:
            return
        index = self.indexAt(event.position().toPoint())
        if not index.isValid():
            target = self.rowCount() - 1          # below the last row
        else:
            above = (self.dropIndicatorPosition()
                     == QAbstractItemView.DropIndicatorPosition.AboveItem)
            before = index.row() if above else index.row() + 1
            # `before` counts the dragged row itself; the move pops it first.
            target = before - 1 if source < before else before
        if target != source:
            self.move_requested.emit(source, target)


class _ElidedLabel(QLabel):
    """A file name cut to the chip's width with an ellipsis (the full path is
    the chip's tooltip)."""

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored,
                           QSizePolicy.Policy.Preferred)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setText(self.fontMetrics().elidedText(
            self._full, Qt.TextElideMode.ElideRight, self.width()))


class _TrackCell(QWidget):
    """One song's backing ('audio') or guide: a chip naming the file, whose ✕
    removes it, or — with none — a dashed button to pick one."""

    def __init__(self, kind, path, on_pick, on_clear, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 0, 6, 0)
        if not path:
            pick = QPushButton("+ Pick file…")
            pick.setObjectName("PlaylistPick")
            pick.setCursor(Qt.CursorShape.PointingHandCursor)
            pick.clicked.connect(on_pick)
            layout.addWidget(pick, 0, Qt.AlignmentFlag.AlignLeft)
            layout.addStretch(1)
            return
        missing = not os.path.exists(path)
        chip = QFrame()
        chip.setObjectName("PlaylistChip")
        chip.setProperty("kind", kind)       # the dot's colour, as in SONG FILES
        chip.setProperty("state", "missing" if missing else "ok")
        chip.setToolTip(f"Missing: {path}" if missing else path)
        row = QHBoxLayout(chip)
        row.setContentsMargins(6, 0, 2, 0)
        row.setSpacing(5)
        dot = QLabel()
        dot.setObjectName("PlaylistChipDot")
        if missing:
            dot.setPixmap(icons.pixmap("warning", 11, "#e24a4a"))
        row.addWidget(dot, 0, Qt.AlignmentFlag.AlignVCenter)
        name = _ElidedLabel(os.path.basename(path))
        name.setObjectName("PlaylistChipName")
        row.addWidget(name, 1)
        clear = QToolButton()
        clear.setObjectName("PlaylistChipClear")
        clear.setIcon(icons.icon("close", "#7a7a7a"))
        clear.setIconSize(QSize(10, 10))
        clear.setToolTip("Remove this track from the song (the file stays on disk)")
        clear.setCursor(Qt.CursorShape.PointingHandCursor)
        clear.clicked.connect(on_clear)
        row.addWidget(clear)
        layout.addWidget(chip)


def _centered(widget):
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(widget, 0, Qt.AlignmentFlag.AlignCenter)
    return holder


class PlaylistDialog(QDialog):
    """The whole setlist in one table.

    Modal, and without OK/Cancel: every edit goes to the controller
    (ui.playlist_controller.PlaylistController) the moment it is made, which
    routes the current song through its own paths and any other song straight
    to the setlist. Rows follow the setlist index; any change to its shape
    rebuilds them from it."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setObjectName("PlaylistDialog")
        self.setModal(True)
        name = controller.show_name()
        self.setWindowTitle(f"Playlist — {name}" if name else "Playlist")

        self._populating = False
        self._last_dir = ""          # the pickers reopen where the last pick was
        self._pending_bpm = {}       # song id -> (song, bpm) awaiting the debounce
        self._bpm_timer = QTimer(self)
        self._bpm_timer.setSingleShot(True)
        self._bpm_timer.setInterval(BPM_COMMIT_MS)
        self._bpm_timer.timeout.connect(self._commit_bpm)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_actions())
        self.table = _SongTable(self)
        layout.addWidget(self.table, 1)
        layout.addWidget(self._build_footer())

        self.table.move_requested.connect(self._move)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._refresh_actions)
        QShortcut(QKeySequence("Ctrl+Up"), self, activated=lambda: self._step(-1))
        QShortcut(QKeySequence("Ctrl+Down"), self, activated=lambda: self._step(+1))

        fit_to_screen(self, 1260, 700)
        self._rebuild()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_actions(self):
        bar = QFrame()
        bar.setObjectName("PlaylistActions")
        row = QHBoxLayout(bar)

        self.new_button = QPushButton("New song")
        self.new_button.setObjectName("PlaylistPrimary")
        self.new_button.setIcon(icons.icon("plus", "#082230"))
        self.new_button.clicked.connect(self._new_song)
        row.addWidget(self.new_button)

        self.import_button = QPushButton("Import from other projects…")
        self.import_button.setObjectName("PlaylistSecondary")
        self.import_button.setIcon(icons.icon("download", "#cfcfcf"))
        self.import_button.clicked.connect(self._import)
        row.addWidget(self.import_button)

        separator = QFrame()
        separator.setObjectName("PlaylistActionsSep")
        separator.setFrameShape(QFrame.Shape.VLine)
        row.addWidget(separator)

        self.up_button = QToolButton()
        self.up_button.setObjectName("PlaylistMove")
        self.up_button.setIcon(icons.icon("chevron-up", "#cfcfcf"))
        self.up_button.setToolTip("Move up (Ctrl+↑)")
        self.up_button.clicked.connect(lambda: self._step(-1))
        row.addWidget(self.up_button)

        self.down_button = QToolButton()
        self.down_button.setObjectName("PlaylistMove")
        self.down_button.setIcon(icons.icon("chevron-down", "#cfcfcf"))
        self.down_button.setToolTip("Move down (Ctrl+↓)")
        self.down_button.clicked.connect(lambda: self._step(+1))
        row.addWidget(self.down_button)

        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("PlaylistDelete")
        self.delete_button.setToolTip(
            "Remove the selected song from the playlist (its files stay on disk)")
        self.delete_button.clicked.connect(self._delete)
        row.addWidget(self.delete_button)

        row.addStretch(1)

        self.filter_edit = QLineEdit()
        self.filter_edit.setObjectName("PlaylistFilter")
        self.filter_edit.setPlaceholderText("Filter songs")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.addAction(icons.icon("search", "#7a7a7a"),
                                   QLineEdit.ActionPosition.LeadingPosition)
        self.filter_edit.textChanged.connect(self._apply_filter)
        row.addWidget(self.filter_edit)
        return bar

    def _build_footer(self):
        footer = QFrame()
        footer.setObjectName("PlaylistFooter")
        row = QHBoxLayout(footer)
        self.count_label = QLabel()
        self.count_label.setObjectName("PlaylistFooterText")
        row.addWidget(self.count_label)
        row.addStretch(1)
        self.hint_label = QLabel("Drag rows or use Ctrl+↑/↓ to reorder")
        self.hint_label.setObjectName("PlaylistFooterText")
        row.addWidget(self.hint_label)
        return footer

    # ------------------------------------------------------------------
    # Rows
    # ------------------------------------------------------------------
    def _rebuild(self, select_id=None):
        """Lay every row out again from the setlist, keeping the selection
        (or selecting `select_id`) and the filter."""
        self._commit_bpm()
        if select_id is None:
            select_id = self._selected_id()
        songs = self.controller.songs()
        self._populating = True
        self.table.setRowCount(0)
        self.table.setRowCount(len(songs))
        for row, song in enumerate(songs):
            self._fill_row(row, song)
        self._populating = False
        for row, song in enumerate(songs):
            if song.id == select_id:
                self.table.selectRow(row)
                break
        self._apply_filter()
        self._refresh_footer()

    def _fill_row(self, row, song):
        table = self.table
        grip = QTableWidgetItem()
        grip.setIcon(icons.icon("drag", "#5f5f5f"))
        grip.setFlags(_ROW_FLAGS)
        grip.setData(_SONG_ID, song.id)
        grip.setToolTip("Drag to reorder")
        table.setItem(row, COL_GRIP, grip)

        on = QCheckBox()
        on.setObjectName("PlaylistOn")
        on.setChecked(song.enabled)
        on.setToolTip("Off: the song stays in the playlist, but next/prev, the "
                      "segue and the web remote skip it")
        on.toggled.connect(lambda checked, s=song: self._set_enabled(s, checked))
        table.setCellWidget(row, COL_ON, _centered(on))

        number = QTableWidgetItem(f"{row + 1:02d}")
        number.setFlags(_ROW_FLAGS)
        number.setForeground(_DIM)
        font = table.font()
        font.setFamily(QFontDatabase.systemFont(
            QFontDatabase.SystemFont.FixedFont).family())
        number.setFont(font)
        table.setItem(row, COL_NUM, number)

        name = QTableWidgetItem(song.title or _placeholder(row))
        name.setFlags(_ROW_FLAGS | Qt.ItemFlag.ItemIsEditable)
        name.setToolTip("Double-click to rename")
        table.setItem(row, COL_NAME, name)
        self._style_name(row, song)

        for col, kind in ((COL_GUIDE, "guide"), (COL_BACKING, "audio")):
            path = song.guide if kind == "guide" else song.audio
            table.setCellWidget(row, col, _TrackCell(
                kind, path,
                on_pick=lambda _=False, s=song, k=kind: self._pick_track(s, k),
                on_clear=lambda _=False, s=song, k=kind: self._clear_track(s, k)))

        bpm = QSpinBox()
        bpm.setObjectName("PlaylistBpm")
        bpm.setRange(MIN_BPM, MAX_BPM)
        bpm.setKeyboardTracking(False)
        bpm.setValue(self.controller.song_bpm(song))
        bpm.valueChanged.connect(lambda value, s=song: self._queue_bpm(s, value))
        bpm.editingFinished.connect(self._commit_bpm)
        table.setCellWidget(row, COL_BPM, _centered(bpm))

        key = QComboBox()
        key.setObjectName("PlaylistKey")
        key.setEditable(True)
        key.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        key.addItems(KEYS)
        if song.key not in KEYS:
            key.addItem(song.key)
        key.setCurrentText(song.key)
        key.activated.connect(
            lambda _i, s=song, c=key: self._set_key(s, c.currentText()))
        key.lineEdit().editingFinished.connect(
            lambda s=song, c=key: self._set_key(s, c.currentText()))
        table.setCellWidget(row, COL_KEY, _centered(key))

        precount = ToggleSwitch()
        precount.setChecked(song.precount)
        precount.setToolTip("Count-in before the song starts")
        precount.toggled.connect(
            lambda checked, s=song: self.controller.set_song_precount(s, checked))
        table.setCellWidget(row, COL_PRECOUNT, _centered(precount))

    def _style_name(self, row, song):
        """A disabled song is struck through; an untitled one shows its
        placeholder dimmed."""
        item = self.table.item(row, COL_NAME)
        font = self.table.font()
        font.setStrikeOut(not song.enabled)
        item.setFont(font)
        if not song.enabled or not song.title:
            item.setForeground(_DIM)
        else:
            item.setData(Qt.ItemDataRole.ForegroundRole, None)

    def _refresh_row(self, song):
        row = self._row_of(song)
        if row < 0:
            return
        self._populating = True
        self._fill_row(row, song)
        self._populating = False

    def _refresh_footer(self):
        songs = self.controller.songs()
        enabled = sum(1 for s in songs if s.enabled)
        self.count_label.setText(
            f"{len(songs)} song{'s' if len(songs) != 1 else ''} · "
            f"{enabled} enabled · {len(songs) - enabled} disabled")

    def _refresh_actions(self):
        row = self._selected_row()
        visible = row >= 0 and not self.table.isRowHidden(row)
        movable = visible and not self._filtering()
        self.up_button.setEnabled(movable and row > 0)
        self.down_button.setEnabled(movable and row < self.table.rowCount() - 1)
        self.delete_button.setEnabled(visible)

    # ------------------------------------------------------------------
    # Selection and filter
    # ------------------------------------------------------------------
    def _selected_row(self):
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def _selected_id(self):
        row = self._selected_row()
        item = self.table.item(row, COL_GRIP) if row >= 0 else None
        return item.data(_SONG_ID) if item is not None else None

    def _row_of(self, song):
        for row, candidate in enumerate(self.controller.songs()):
            if candidate is song:
                return row
        return -1

    def _filtering(self):
        return bool(self.filter_edit.text().strip())

    def _apply_filter(self):
        """Hide the songs whose title does not contain the filter text. While
        it hides any, reordering is off: "one place up" past a hidden row is
        not a move anyone could predict."""
        needle = self.filter_edit.text().strip().casefold()
        for row, song in enumerate(self.controller.songs()):
            title = (song.title or _placeholder(row)).casefold()
            self.table.setRowHidden(row, bool(needle) and needle not in title)
        self.table.setDragEnabled(not needle)
        self.hint_label.setText("Clear the filter to reorder" if needle
                                else "Drag rows or use Ctrl+↑/↓ to reorder")
        self._refresh_actions()

    # ------------------------------------------------------------------
    # Edits
    # ------------------------------------------------------------------
    def _new_song(self):
        index = self.controller.new_song()
        song = self.controller.songs()[index]
        self.filter_edit.clear()           # the new row must not be filtered out
        self._rebuild(select_id=song.id)
        item = self.table.item(index, COL_NAME)
        self.table.scrollToItem(item)
        self.table.setFocus()
        self.table.editItem(item)

    def _delete(self):
        row = self._selected_row()
        if row < 0:
            return
        song = self.controller.songs()[row]
        owned = self.controller.song_owned_files(song)
        box = QMessageBox(self)
        box.setWindowTitle("Delete song")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"Remove “{song.title or _placeholder(row)}” from the playlist?")
        if owned:
            box.setInformativeText(
                "Keep its files, or delete them too? Deleting is permanent:\n"
                + "".join(f"\n    • {os.path.basename(p)}" for p in owned)
                + "\n\nFiles outside the show folder, or used by another song, "
                  "are always kept.")
            keep = box.addButton("Remove, keep files",
                                 QMessageBox.ButtonRole.AcceptRole)
            delete = box.addButton("Remove and delete files",
                                   QMessageBox.ButtonRole.DestructiveRole)
        else:
            if song.midi or song.audio or song.guide:
                box.setInformativeText(
                    "Its files are outside the show folder or used by another "
                    "song, so they stay on disk.")
            keep = box.addButton("Remove", QMessageBox.ButtonRole.AcceptRole)
            delete = None
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(keep)
        box.exec()
        clicked = box.clickedButton()
        # isVisible: another show opened while the question was up closes
        # this dialog, and `row` would index that show's setlist.
        if clicked not in (keep, delete) or not self.isVisible():
            return
        failed = self.controller.remove_song(row, delete_files=clicked is delete)
        songs = self.controller.songs()
        self._rebuild(select_id=songs[min(row, len(songs) - 1)].id if songs else None)
        if failed:
            QMessageBox.warning(self, "Delete song",
                                "The song is gone, but these files could not "
                                "be deleted:\n\n" + "\n".join(failed))

    def _move(self, src, dst):
        if self._filtering():
            return
        song_id = self.controller.songs()[src].id
        self.controller.move_song(src, dst)
        self._rebuild(select_id=song_id)

    def _step(self, delta):
        row = self._selected_row()
        if row < 0 or self._filtering():
            return
        if 0 <= row + delta < len(self.controller.songs()):
            self._move(row, row + delta)

    def _on_item_changed(self, item):
        if self._populating or item.column() != COL_NAME:
            return
        row = item.row()
        song = self.controller.songs()[row]
        text = item.text().strip()
        if not song.title and text == _placeholder(row):
            return                          # the placeholder, left as it was
        if text != song.title:
            self.controller.set_song_title(song, text)
        self._populating = True
        item.setText(song.title or _placeholder(row))
        self._style_name(row, song)
        self._populating = False
        self._apply_filter()

    def _set_enabled(self, song, enabled):
        self.controller.set_song_enabled(song, enabled)
        row = self._row_of(song)
        if row >= 0:
            self._populating = True
            self._style_name(row, song)
            self._populating = False
        self._refresh_footer()

    def _set_key(self, song, key):
        key = key.strip()
        if key != song.key:
            self.controller.set_song_key(song, key)

    def _queue_bpm(self, song, bpm):
        if self._populating:
            return
        self._pending_bpm[song.id] = (song, bpm)
        self._bpm_timer.start()

    def _commit_bpm(self):
        self._bpm_timer.stop()
        pending, self._pending_bpm = self._pending_bpm, {}
        for song, bpm in pending.values():
            self.controller.set_song_bpm(song, bpm)

    def _pick_track(self, song, kind):
        caption = "Choose guide track" if kind == "guide" else "Choose backing track"
        path, _ = QFileDialog.getOpenFileName(self, caption, self._last_dir,
                                              AUDIO_FILTER)
        if not path:
            return
        self._last_dir = os.path.dirname(path)
        self.controller.set_song_track(song, kind, path)
        self._refresh_row(song)

    def _clear_track(self, song, kind):
        self.controller.set_song_track(song, kind, None)
        self._refresh_row(song)

    def _import(self):
        picker = ImportSongsDialog(self.controller.other_shows(),
                                   self.controller.patch(), self)
        accepted = picker.exec() == QDialog.DialogCode.Accepted
        songs, copy_audio = picker.selected_songs(), picker.copy_audio()
        picker.deleteLater()
        if not accepted or not songs or not self.isVisible():
            return                          # (not visible: another show opened)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            new_songs, warnings = self.controller.import_songs(songs, copy_audio)
        except OSError as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Import failed",
                                 f"The songs could not be imported:\n\n{e}")
            return
        QApplication.restoreOverrideCursor()
        self.filter_edit.clear()
        self._rebuild(select_id=new_songs[0].id if new_songs else None)
        if new_songs:
            self.table.scrollToItem(self.table.item(self._selected_row(), COL_NAME))
        if warnings:
            QMessageBox.warning(
                self, "Import",
                f"Imported {len(new_songs)} song(s), with problems:\n\n"
                + "\n".join(warnings))

    def done(self, result):
        """Closing writes a BPM still waiting for its debounce."""
        self._commit_bpm()
        super().done(result)
