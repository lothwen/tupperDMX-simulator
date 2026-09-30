# ABOUTME: ImportSongsDialog — pick songs from the other shows in the shows
# ABOUTME: folder (checks kept per show, so one import can draw on several),
# ABOUTME: warned when a show's patch disagrees, and asked how audio comes in.
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from core.config import SongConfig
from core.show import song_import
from core.show.model import SHOW_DIR_SUFFIX, ShowError
from ui.widgets import icons
from ui.widgets.sizing import fit_to_screen
from webremote.state import probe_bpm

_COL_TITLE, _COL_BPM, _COL_KEY = range(3)


@dataclass
class _Source:
    """One show beside the open one, read (never opened) for importing."""
    root: Path
    name: str
    songs: List[SongConfig] = field(default_factory=list)
    patch_differences: List[int] = field(default_factory=list)
    error: Optional[str] = None


def _read_source(root, target_patch) -> _Source:
    name = root.name[:-len(SHOW_DIR_SUFFIX)] if root.name.endswith(SHOW_DIR_SUFFIX) \
        else root.name
    try:
        show, raw_patch = song_import.peek_show(root)
    except ShowError as e:
        return _Source(root, name, error=str(e))
    return _Source(root, show.name, list(show.playlist.songs),
                   song_import.patch_differences(raw_patch, target_patch))


class ImportSongsDialog(QDialog):
    """The songs of the other shows, to check and import.

    A picker only: it reads the shows (song_import.peek_show — never opening
    them) and hands back the checked songs and whether their audio should be
    copied; the playlist manager does the importing."""

    def __init__(self, show_roots, target_patch, parent=None):
        super().__init__(parent)
        self.setObjectName("ImportSongsDialog")
        self.setWindowTitle("Import from other projects")
        self.setModal(True)
        self._sources = [_read_source(Path(r), target_patch) for r in show_roots]
        self._checked = {}            # source root -> set of checked song ids
        self._current: Optional[_Source] = None
        self._populating = False
        self._copy_audio = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        body = QHBoxLayout()
        body.setSpacing(0)
        body.addWidget(self._build_projects())
        body.addWidget(self._build_songs(), 1)
        layout.addLayout(body, 1)
        layout.addWidget(self._build_footer())

        fit_to_screen(self, 720, 460)
        first = next((i for i, s in enumerate(self._sources) if s.error is None), -1)
        if first >= 0:
            self.projects.setCurrentRow(first)
        else:
            self._show_source(None)
        self._refresh_status()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_projects(self):
        panel = QFrame()
        panel.setObjectName("ImportProjectsPanel")
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        heading = QLabel("PROJECTS")
        heading.setObjectName("ImportHeading")
        column.addWidget(heading)

        self.projects = QListWidget()
        self.projects.setObjectName("ImportProjects")
        for source in self._sources:
            item = QListWidgetItem()
            entry = QWidget()
            entry.setObjectName("ImportProjectEntry")
            lines = QVBoxLayout(entry)
            name = QLabel(source.name)
            name.setObjectName("ImportProjectName")
            count = len(source.songs)
            detail = QLabel("Can't be read" if source.error
                            else f"{count} song{'s' if count != 1 else ''}")
            detail.setObjectName("ImportProjectDetail")
            lines.addWidget(name)
            lines.addWidget(detail)
            if source.error:
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                entry.setToolTip(source.error)
                entry.setEnabled(False)
            item.setSizeHint(entry.sizeHint())
            self.projects.addItem(item)
            self.projects.setItemWidget(item, entry)
        self.projects.currentRowChanged.connect(
            lambda row: self._show_source(
                self._sources[row] if 0 <= row < len(self._sources) else None))
        column.addWidget(self.projects, 1)
        return panel

    def _build_songs(self):
        panel = QFrame()
        panel.setObjectName("ImportSongsPanel")
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        header = QFrame()
        header.setObjectName("ImportSongsHeader")
        row = QHBoxLayout(header)
        self.select_all = QCheckBox("Select all")
        self.select_all.setObjectName("ImportSelectAll")
        self.select_all.clicked.connect(self._toggle_all)
        row.addWidget(self.select_all)
        row.addStretch(1)
        column.addWidget(header)

        self.patch_warning = QLabel()
        self.patch_warning.setObjectName("ImportPatchWarning")
        self.patch_warning.setWordWrap(True)
        self.patch_warning.hide()
        column.addWidget(self.patch_warning)

        self.song_list = QTreeWidget()
        self.song_list.setObjectName("ImportSongList")
        self.song_list.setColumnCount(3)
        self.song_list.setHeaderHidden(True)
        self.song_list.setRootIsDecorated(False)
        self.song_list.setIndentation(0)
        self.song_list.setSelectionMode(QTreeWidget.SelectionMode.NoSelection)
        self.song_list.itemChanged.connect(self._on_item_changed)
        column.addWidget(self.song_list, 1)

        self.empty_label = QLabel("No other projects in the shows folder.")
        self.empty_label.setObjectName("ImportEmpty")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.hide()
        column.addWidget(self.empty_label, 1)
        return panel

    def _build_footer(self):
        footer = QFrame()
        footer.setObjectName("ImportFooter")
        row = QHBoxLayout(footer)
        self.status_label = QLabel()
        self.status_label.setObjectName("ImportStatus")
        row.addWidget(self.status_label)
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("ImportCancel")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        self.import_button = QPushButton("Import")
        self.import_button.setObjectName("ImportConfirm")
        self.import_button.setDefault(True)
        self.import_button.clicked.connect(self._on_import)
        row.addWidget(self.import_button)
        return footer

    # ------------------------------------------------------------------
    # The songs of one show
    # ------------------------------------------------------------------
    def _show_source(self, source):
        self._current = source
        self._populating = True
        self.song_list.clear()
        has_songs = source is not None and bool(source.songs)
        self.song_list.setVisible(source is not None)
        self.empty_label.setVisible(source is None)
        self.select_all.setEnabled(has_songs)
        if source is not None:
            checked = self._checked.get(source.root, set())
            for song in source.songs:
                bpm = probe_bpm(song.midi) or song.cache.get("bpm")
                item = QTreeWidgetItem([song.title or "Untitled",
                                        str(bpm) if bpm else "—", song.key or ""])
                item.setFlags(Qt.ItemFlag.ItemIsEnabled
                              | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(_COL_TITLE, Qt.CheckState.Checked
                                   if song.id in checked else Qt.CheckState.Unchecked)
                item.setData(_COL_TITLE, Qt.ItemDataRole.UserRole, song.id)
                for col in (_COL_BPM, _COL_KEY):
                    item.setForeground(col, Qt.GlobalColor.gray)
                self.song_list.addTopLevelItem(item)
            header = self.song_list.header()
            header.setStretchLastSection(False)
            header.setSectionResizeMode(_COL_TITLE, QHeaderView.ResizeMode.Stretch)
            for col in (_COL_BPM, _COL_KEY):
                header.setSectionResizeMode(
                    col, QHeaderView.ResizeMode.ResizeToContents)
        differences = source.patch_differences if source is not None else []
        if differences:
            channels = ", ".join(str(c) for c in differences)
            self.patch_warning.setText(
                f'{icons.img_tag("warning", "#d9a13b", 12)} This project\'s patch '
                f"differs on MIDI channel{'s' if len(differences) > 1 else ''} "
                f"{channels}: its songs' cues there will drive other fixtures "
                "here (or none).")
        self.patch_warning.setVisible(bool(differences))
        self._populating = False
        self._refresh_select_all()

    def _on_item_changed(self, item, column):
        if self._populating or column != _COL_TITLE or self._current is None:
            return
        checked = self._checked.setdefault(self._current.root, set())
        song_id = item.data(_COL_TITLE, Qt.ItemDataRole.UserRole)
        if item.checkState(_COL_TITLE) == Qt.CheckState.Checked:
            checked.add(song_id)
        else:
            checked.discard(song_id)
        self._refresh_select_all()
        self._refresh_status()

    def _toggle_all(self):
        """Check every song of this show — or, when all already are, none."""
        items = [self.song_list.topLevelItem(i)
                 for i in range(self.song_list.topLevelItemCount())]
        all_checked = items and all(
            i.checkState(_COL_TITLE) == Qt.CheckState.Checked for i in items)
        state = Qt.CheckState.Unchecked if all_checked else Qt.CheckState.Checked
        for item in items:
            item.setCheckState(_COL_TITLE, state)
        self._refresh_select_all()

    def _refresh_select_all(self):
        items = [self.song_list.topLevelItem(i)
                 for i in range(self.song_list.topLevelItemCount())]
        count = sum(1 for i in items
                    if i.checkState(_COL_TITLE) == Qt.CheckState.Checked)
        self.select_all.setCheckState(
            Qt.CheckState.Unchecked if count == 0 else
            Qt.CheckState.Checked if count == len(items) else
            Qt.CheckState.PartiallyChecked)

    def _refresh_status(self):
        songs = len(self.selected_songs())
        shows = sum(1 for ids in self._checked.values() if ids)
        if songs == 0:
            text = "Select songs to import"
        else:
            text = f"{songs} song{'s' if songs != 1 else ''} selected"
            if shows > 1:
                text += f" from {shows} projects"
        self.status_label.setText(text)
        self.import_button.setEnabled(songs > 0)

    # ------------------------------------------------------------------
    # Result
    # ------------------------------------------------------------------
    def selected_songs(self) -> List[SongConfig]:
        """The checked songs, show by show in the list's order, each show's in
        its setlist order."""
        out = []
        for source in self._sources:
            ids = self._checked.get(source.root, set())
            out.extend(s for s in source.songs if s.id in ids)
        return out

    def copy_audio(self) -> bool:
        return self._copy_audio

    def _on_import(self):
        """Ask how the audio comes in — only the user knows whether this show
        should stand alone — then close."""
        if any(s.audio or s.guide for s in self.selected_songs()):
            box = QMessageBox(self)
            box.setWindowTitle("Import audio")
            box.setIcon(QMessageBox.Icon.Question)
            box.setText("How should the songs' audio files come in?")
            box.setInformativeText(
                "Copy: the wav/mp3 files are copied into this show's audio "
                "folder, so it keeps working if the other project is moved or "
                "deleted.\n\nLink: the songs keep using the original files — "
                "nothing is copied, but they go missing if those files move.")
            copy = box.addButton("Copy into this show",
                                 QMessageBox.ButtonRole.AcceptRole)
            link = box.addButton("Link to original files",
                                 QMessageBox.ButtonRole.AcceptRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(copy)
            box.exec()
            clicked = box.clickedButton()
            if clicked is copy:
                self._copy_audio = True
            elif clicked is link:
                self._copy_audio = False
            else:
                return
        self.accept()
