import html
from datetime import datetime
from typing import Callable, Optional
from PyQt6.QtWidgets import (QToolBar, QLabel, QWidget, QSizePolicy,
                             QFrame, QHBoxLayout, QPushButton, QLineEdit,
                             QVBoxLayout, QToolButton, QMenu, QComboBox)
from PyQt6.QtGui import QAction, QFont
from PyQt6.QtCore import QObject, pyqtSignal, Qt

from tupperlogger import get_logger
from core.config import Config
from webremote.state import probe_duration


class ToolbarManager(QObject):
    """Manages the main application toolbar with media controls and configuration options."""

    # Signals
    load_file_requested = pyqtSignal(str)  # file_type ('midi', 'audio' or 'guide')
    guide_clear_requested = pyqtSignal()  # remove the current song's guide track
    new_midi_track_requested = pyqtSignal()  # request to create new empty MIDI track
    export_midi_requested = pyqtSignal()  # request to export MIDI file
    # Show project signals
    new_show_requested = pyqtSignal()
    open_show_requested = pyqtSignal()
    save_show_requested = pyqtSignal()
    save_show_as_requested = pyqtSignal()
    # Version signals
    snapshot_requested = pyqtSignal()
    restore_version_requested = pyqtSignal(str)  # restore the picked version_id
    # Bundle / setlist signals
    export_bundle_requested = pyqtSignal()
    import_bundle_requested = pyqtSignal()
    song_title_edited = pyqtSignal(str)
    song_key_edited = pyqtSignal(str)        # current song's musical key (TONO)
    # Playlist navigation signals
    prev_song_requested = pyqtSignal()
    next_song_requested = pyqtSignal()
    remove_song_requested = pyqtSignal()
    song_selected = pyqtSignal(int)  # setlist combobox picked a song by index
    # Application menu signals
    settings_requested = pyqtSignal()
    about_requested = pyqtSignal()

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.logger = get_logger("ui.toolbar")

        # Toolbar and widgets
        self.toolbar: Optional[QToolBar] = None
        self.export_midi_action: Optional[QAction] = None
        # Song-files cards (path wells + empty-state load actions)
        self.audio_file_label: Optional[QLabel] = None
        self.audio_empty_button: Optional[QPushButton] = None
        self.guide_file_label: Optional[QLabel] = None
        self.guide_empty_button: Optional[QPushButton] = None
        self.guide_clear_button: Optional[QToolButton] = None
        self.midi_file_label: Optional[QLabel] = None
        self.midi_empty_button: Optional[QPushButton] = None
        # Playlist navigation widgets
        self.prev_button: Optional[QPushButton] = None
        self.next_button: Optional[QPushButton] = None
        self.remove_button: Optional[QPushButton] = None
        self.menu_button: Optional[QToolButton] = None
        # Show section widgets
        self.show_name_label: Optional[QLabel] = None
        self.autosave_status_label: Optional[QLabel] = None
        self.show_stats_label: Optional[QLabel] = None
        # Setlist widgets
        self.setlist_count_label: Optional[QLabel] = None
        self.setlist_counter_badge: Optional[QLabel] = None
        self.song_combo: Optional[QComboBox] = None
        self._populating_setlist = False
        self.bpm_label: Optional[QLabel] = None
        self.offset_label: Optional[QLabel] = None
        self.linked_chip: Optional[QLabel] = None
        self.key_edit: Optional[QLineEdit] = None
        # Versions widgets
        self.versions_combo: Optional[QComboBox] = None
        self.restore_button: Optional[QPushButton] = None
        self.versions_stats_label: Optional[QLabel] = None

        self.logger.info("Toolbar manager initialized")
    
    def create_toolbar(self, parent) -> QToolBar:
        """Create and setup the main toolbar with grouped sections."""
        self.toolbar = QToolBar("Main Toolbar", parent)
        self.toolbar.setMinimumHeight(115)
        self.toolbar.setMovable(False)
        self.toolbar.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        
        # Create a container widget with horizontal layout to properly manage expansion
        container = QWidget()
        container_layout = QHBoxLayout(container)
        container_layout.setContentsMargins(10, 10, 10, 10)
        container_layout.setSpacing(15)
        
        # Show Section (far left - fixed width project controls)
        show_section = self._create_show_section()
        container_layout.addWidget(show_section, 0)

        # Setlist Section (playlist navigation + song selector)
        setlist_section = self._create_setlist_section()
        container_layout.addWidget(setlist_section, 2)

        # Song Files Section (per-song audio/MIDI loading + info)
        song_files_section = self._create_song_files_section()
        container_layout.addWidget(song_files_section, 3)  # widest — long paths

        # Versions Section (right of files - snapshot/history controls)
        versions_section = self._create_versions_section()
        container_layout.addWidget(versions_section, 0)

        # Menu Section (far right - fixed width hamburger button)
        menu_section = self._create_menu_section()
        container_layout.addWidget(menu_section, 0)

        # Add the container to the toolbar
        self.toolbar.addWidget(container)
        
        self.logger.info("Main toolbar created")
        return self.toolbar
    
    def _create_show_section(self) -> QFrame:
        """Create the show-project section: SHOW header,
        New/Open/Save/Save as… buttons, a "…" overflow menu (bundle
        import), the split Export button (bundle by default, MIDI/bundle
        menu), and a footer with the show name plus setlist/patch stats.
        (The live autosave chip lives in the SONG FILES header — the
        debounce it reports is the song's .mid autosave.)"""
        section = QFrame()
        section.setFrameStyle(QFrame.Shape.StyledPanel)
        section.setObjectName("ToolbarSection")

        layout = QVBoxLayout(section)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(4)

        header_layout = QHBoxLayout()
        header_layout.setSpacing(8)
        title_label = QLabel("SHOW")
        title_label.setObjectName("ShowSectionTitle")
        font = title_label.font()
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.5)
        title_label.setFont(font)
        header_layout.addWidget(title_label)
        header_layout.addStretch(1)
        layout.addLayout(header_layout)

        buttons_layout = QHBoxLayout()
        for text, tooltip, signal in (
                ("+ New", "Create a new show project", self.new_show_requested),
                ("📂 Open", "Open a show folder", self.open_show_requested),
                ("💾 Save", "Save the show now (autosave already runs)", self.save_show_requested),
                ("Save as…", "Duplicate the show under a new name", self.save_show_as_requested)):
            button = QPushButton(text)
            button.setFixedHeight(30)
            button.setObjectName("LoadButton")
            button.setToolTip(tooltip)
            button.clicked.connect(signal.emit)
            buttons_layout.addWidget(button)
        more_button = QToolButton()
        more_button.setText("…")
        more_button.setFixedSize(34, 30)
        more_button.setObjectName("MenuButton")
        more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        more_button.setToolTip("More show actions")
        more_menu = QMenu(more_button)
        import_action = more_menu.addAction("Import bundle…")
        import_action.setToolTip("Import a portable .showpack bundle")
        import_action.triggered.connect(self.import_bundle_requested.emit)
        more_button.setMenu(more_menu)
        buttons_layout.addWidget(more_button)

        buttons_layout.addSpacing(22)
        export_button = QToolButton()
        export_button.setText("⤓  Export")
        export_button.setFixedHeight(30)
        # The styled ▾ segment + padding aren't in Qt's size hint — reserve
        # room so the label doesn't elide.
        export_button.setMinimumWidth(112)
        export_button.setObjectName("ExportSplitButton")
        export_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        export_button.setToolTip(
            "Export the show as a portable .showpack bundle — the ▾ menu "
            "also offers a standard-MIDI copy of the current song")
        export_menu = QMenu(export_button)
        export_menu.setToolTipsVisible(True)
        self.export_midi_action = export_menu.addAction("⤓  Export MIDI…")
        self.export_midi_action.setToolTip(
            "Standard-MIDI interchange copy of the current song (the show "
            "itself autosaves)")
        self.export_midi_action.triggered.connect(self.export_midi_requested.emit)
        self.export_midi_action.setEnabled(False)  # Disabled by default
        bundle_action = export_menu.addAction("📦  Export bundle")
        bundle_action.setToolTip(
            "One portable .showpack with audio, MIDI, patch and fixtures "
            "(all songs)")
        bundle_action.triggered.connect(self.export_bundle_requested.emit)
        export_button.setMenu(export_menu)
        export_button.clicked.connect(self.export_bundle_requested.emit)
        buttons_layout.addWidget(export_button)
        layout.addLayout(buttons_layout)

        info_layout = QHBoxLayout()
        info_layout.setSpacing(8)
        self.show_name_label = QLabel("No show")
        self.show_name_label.setObjectName("ShowNameLabel")
        info_layout.addWidget(self.show_name_label)
        info_layout.addStretch(1)
        self.show_stats_label = QLabel("")
        self.show_stats_label.setObjectName("ShowStatsLabel")
        self.show_stats_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        info_layout.addWidget(self.show_stats_label)
        layout.addLayout(info_layout)
        self.update_show_stats()

        self.logger.debug("Show section created")
        return section

    def _create_versions_section(self) -> QFrame:
        """Create the VERSIONS section: header, a row with a snapshot picker +
        Restore, and a footer with the pinned-snapshot action plus
        autosave/pinned counts."""
        section = QFrame()
        section.setFrameStyle(QFrame.Shape.StyledPanel)
        section.setObjectName("ToolbarSection")

        layout = QVBoxLayout(section)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(4)

        header_layout = QHBoxLayout()
        title_label = QLabel("VERSIONS")
        title_label.setObjectName("ShowSectionTitle")
        font = title_label.font()
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.5)
        title_label.setFont(font)
        header_layout.addWidget(title_label)
        header_layout.addStretch(1)
        layout.addLayout(header_layout)

        picker_layout = QHBoxLayout()
        picker_layout.setSpacing(6)
        self.versions_combo = QComboBox()
        self.versions_combo.setObjectName("VersionsCombo")
        self.versions_combo.setFixedHeight(30)
        self.versions_combo.setMinimumWidth(185)
        self.versions_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.versions_combo.setToolTip("Pick a snapshot to restore")
        picker_layout.addWidget(self.versions_combo, 1)
        self.restore_button = QPushButton("Restore")
        self.restore_button.setFixedHeight(30)
        self.restore_button.setObjectName("LoadButton")
        self.restore_button.setToolTip("Restore the show to the selected snapshot (undoable)")
        self.restore_button.setEnabled(False)
        self.restore_button.clicked.connect(self._on_restore_clicked)
        picker_layout.addWidget(self.restore_button)
        layout.addLayout(picker_layout)

        footer_layout = QHBoxLayout()
        footer_layout.setSpacing(8)
        snapshot_button = QPushButton("📌 Snapshot")
        snapshot_button.setFixedHeight(26)
        snapshot_button.setObjectName("SnapshotButton")
        snapshot_button.setToolTip("Take a named, never-pruned snapshot")
        snapshot_button.clicked.connect(self.snapshot_requested.emit)
        footer_layout.addWidget(snapshot_button)
        self.versions_stats_label = QLabel("")
        self.versions_stats_label.setObjectName("ShowStatsLabel")
        footer_layout.addWidget(self.versions_stats_label)
        footer_layout.addStretch(1)
        layout.addLayout(footer_layout)

        self.logger.debug("Versions section created")
        return section

    def _on_restore_clicked(self) -> None:
        """Emit a restore request for the version currently picked in the combobox."""
        if self.versions_combo is None:
            return
        version_id = self.versions_combo.currentData()
        if version_id:
            self.restore_version_requested.emit(version_id)

    def set_show_name(self, name: str) -> None:
        """Update the show-name display in the SHOW section (bold name plus a
        dimmed .show suffix)."""
        if self.show_name_label:
            if name:
                self.show_name_label.setText(
                    f"<b>{html.escape(name)}</b>"
                    "<span style='color:#8a8a8a; font-size:11px;'> .show</span>")
            else:
                self.show_name_label.setText("No show")

    def update_show_stats(self) -> None:
        """Refresh the SHOW footer stats: setlist size/total duration and the
        fixture-patch size, straight from the (show-aliased) config."""
        if not self.show_stats_label:
            return
        songs = self.config.playlist.songs
        total = 0.0
        for song in songs:
            duration = song.cache.get("duration") or probe_duration(song.audio)
            if duration:
                total += duration
        minutes, seconds = divmod(int(total), 60)
        fixtures = len(self.config.fixtures)
        self.show_stats_label.setText(
            f"{len(songs)} song{'s' if len(songs) != 1 else ''}"
            f" · {minutes}:{seconds:02d}s\n"
            f"patch {fixtures} fix")

    def set_versions(self, infos: list) -> None:
        """Populate the snapshot picker (newest first, auto-selected) and the
        autosave/pinned counts from a list of `versions.VersionInfo`."""
        if self.versions_combo is None:
            return
        self.versions_combo.blockSignals(True)
        try:
            self.versions_combo.clear()
            autosaves = pinned = 0
            for info in infos:
                if info.pinned:
                    pinned += 1
                else:
                    autosaves += 1
                self.versions_combo.addItem(self._version_label(info), info.version_id)
        finally:
            self.versions_combo.blockSignals(False)
        if infos:
            self.versions_combo.setCurrentIndex(0)
        if self.restore_button:
            self.restore_button.setEnabled(bool(infos))
        if self.versions_stats_label:
            self.versions_stats_label.setText(
                f"{autosaves} autosave{'s' if autosaves != 1 else ''}"
                f" · {pinned} pinned")

    def _version_label(self, info) -> str:
        """Combobox label for a snapshot: '📌 name · time' when pinned, else
        '● Autosave · time'."""
        when = self._format_version_time(info.timestamp)
        if info.pinned:
            return f"📌 {info.name} · {when}" if info.name else f"📌 {when}"
        return f"● Autosave · {when}"

    @staticmethod
    def _format_version_time(timestamp: str) -> str:
        """Snapshot stamp ('YYYY-MM-DDTHH-MM-SS') → clock time for today, else
        'MM-DD HH:MM:SS'. Falls back to the raw stamp if it can't be parsed."""
        try:
            dt = datetime.strptime(timestamp, "%Y-%m-%dT%H-%M-%S")
        except (ValueError, TypeError):
            return str(timestamp).replace("T", " ")
        if dt.date() == datetime.now().date():
            return dt.strftime("%H:%M:%S")
        return dt.strftime("%m-%d %H:%M:%S")

    def set_autosave_state(self, state: str) -> None:
        """Reflect the ShowManager autosave state ('saved'|'pending'|'error')."""
        if not self.autosave_status_label:
            return
        text, color = {
            "saved": ("● All changes saved", "#4caf50"),
            "pending": ("● Editing · autosave in 3s", "#e2a04a"),
            "error": ("⚠ Save error — check logs", "#e24a4a"),
        }.get(state, ("", ""))
        self.autosave_status_label.setText(text)
        self.autosave_status_label.setStyleSheet(f"color: {color};" if color else "")

    def _create_setlist_section(self) -> QFrame:
        """Create the SETLIST section: header (title + song count), a row with
        prev / song selector combobox / next / remove, and a footer with the
        current song's BPM, offset and derived media 'linked' chip."""
        section = QFrame()
        section.setFrameStyle(QFrame.Shape.StyledPanel)
        section.setObjectName("ToolbarSection")

        layout = QVBoxLayout(section)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(4)

        header_layout = QHBoxLayout()
        title_label = QLabel("SETLIST")
        title_label.setObjectName("ShowSectionTitle")
        font = title_label.font()
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.5)
        title_label.setFont(font)
        header_layout.addWidget(title_label)
        header_layout.addStretch(1)
        self.setlist_count_label = QLabel("0 songs")
        self.setlist_count_label.setObjectName("ShowStatsLabel")
        header_layout.addWidget(self.setlist_count_label)
        layout.addLayout(header_layout)

        controls_layout = QHBoxLayout()
        controls_layout.setSpacing(6)

        self.prev_button = QPushButton("⏮")
        self.prev_button.setFixedSize(30, 30)
        self.prev_button.setObjectName("NavButton")
        self.prev_button.setToolTip("Previous song")
        self.prev_button.clicked.connect(self.prev_song_requested.emit)
        self.prev_button.setEnabled(False)
        controls_layout.addWidget(self.prev_button)

        # Unified selector: a counter badge + editable combobox in one rounded
        # frame (the frame carries the border; the combobox is drawn flat).
        selector = QFrame()
        selector.setObjectName("SetlistSelector")
        selector.setFixedHeight(30)
        selector_layout = QHBoxLayout(selector)
        selector_layout.setContentsMargins(4, 0, 2, 0)
        selector_layout.setSpacing(6)
        self.setlist_counter_badge = QLabel("00/00")
        self.setlist_counter_badge.setObjectName("SetlistCounter")
        self.setlist_counter_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        selector_layout.addWidget(self.setlist_counter_badge)
        self.song_combo = QComboBox()
        self.song_combo.setObjectName("SetlistCombo")
        self.song_combo.setEditable(True)
        self.song_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.song_combo.lineEdit().setPlaceholderText("Song title")
        self.song_combo.setToolTip("Select a song, or edit its title and press Enter to rename")
        self.song_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        # Selecting from the dropdown switches song; typing + Enter renames the
        # current one. activated() only fires on user action (not programmatic
        # repopulation), so no guard is needed on the selection path.
        self.song_combo.activated.connect(self._on_combo_activated)
        self.song_combo.lineEdit().returnPressed.connect(
            lambda: self.song_title_edited.emit(self.song_combo.currentText()))
        selector_layout.addWidget(self.song_combo, 1)
        controls_layout.addWidget(selector, 1)

        self.next_button = QPushButton("⏭")
        self.next_button.setFixedSize(30, 30)
        self.next_button.setObjectName("NavButton")
        self.next_button.setToolTip("Next song")
        self.next_button.clicked.connect(self.next_song_requested.emit)
        self.next_button.setEnabled(False)
        controls_layout.addWidget(self.next_button)

        self.remove_button = QPushButton("🗑")
        self.remove_button.setFixedSize(30, 30)
        self.remove_button.setObjectName("RemoveButton")
        self.remove_button.setToolTip("Remove current song from playlist")
        self.remove_button.clicked.connect(self.remove_song_requested.emit)
        controls_layout.addWidget(self.remove_button)

        layout.addLayout(controls_layout)

        footer_layout = QHBoxLayout()
        footer_layout.setSpacing(10)
        self.bpm_label = QLabel("")
        self.bpm_label.setObjectName("BpmLabel")
        footer_layout.addWidget(self.bpm_label)
        self.offset_label = QLabel("")
        self.offset_label.setObjectName("OffsetLabel")
        footer_layout.addWidget(self.offset_label)
        self.linked_chip = QLabel("")
        self.linked_chip.setObjectName("LinkedChip")
        footer_layout.addWidget(self.linked_chip)
        footer_layout.addStretch(1)
        # Per-song live-player field (Fase 4): musical key. (The count-in
        # toggle lives on the timeline toolbar, next to the BPM spinbox.)
        self.key_edit = QLineEdit()
        self.key_edit.setObjectName("SongKeyEdit")
        self.key_edit.setPlaceholderText("key")
        self.key_edit.setFixedWidth(44)
        self.key_edit.setToolTip(
            "Musical key of the song (e.g. Am) — shown on the web remote")
        self.key_edit.editingFinished.connect(
            lambda: self.song_key_edited.emit(self.key_edit.text().strip()))
        footer_layout.addWidget(self.key_edit)
        layout.addLayout(footer_layout)

        self.logger.debug("Setlist section created")
        return section

    def _create_song_files_section(self) -> QFrame:
        """Create the SONG FILES section: header (title + the live autosave
        chip) over three accent-coloured cards — MIDI, BACKING TRACK and
        GUIDE — each with a small 🗁 load button and a path well that swaps
        for a dashed '+ Load …' call-to-action while the slot is empty."""
        section = QFrame()
        section.setFrameStyle(QFrame.Shape.StyledPanel)
        section.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        section.setObjectName("ToolbarSection")

        layout = QVBoxLayout(section)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(4)

        header_layout = QHBoxLayout()
        header_layout.setSpacing(8)
        title_label = QLabel("SONG FILES")
        title_label.setObjectName("ShowSectionTitle")
        font = title_label.font()
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.5)
        title_label.setFont(font)
        header_layout.addWidget(title_label)
        header_layout.addStretch(1)
        self.autosave_status_label = QLabel("")
        self.autosave_status_label.setObjectName("AutosaveStatus")
        header_layout.addWidget(self.autosave_status_label)
        layout.addLayout(header_layout)

        cards_layout = QHBoxLayout()
        cards_layout.setSpacing(8)

        # MIDI card (+ the ▷ new-track header action)
        new_track_button = self._card_button(
            "▷", "Start a new empty MIDI track for this song")
        new_track_button.clicked.connect(self.new_midi_track_requested.emit)
        card, self.midi_file_label, self.midi_empty_button = \
            self._create_file_card(
                "midi", "🎹", "MIDI", 'midi',
                "Load a MIDI file for this song", "+ Load MIDI",
                extras=(new_track_button,))
        cards_layout.addWidget(card, 1)

        # BACKING TRACK card — the live master (song identity, web remote,
        # headless).
        card, self.audio_file_label, self.audio_empty_button = \
            self._create_file_card(
                "backing", "🎵", "BACKING TRACK", 'audio',
                "Load the backing track for this song", "+ Load backing")
        cards_layout.addWidget(card, 1)

        # GUIDE card (+ the ✕ clear header action, shown while loaded) —
        # the optional full-mix design reference.
        self.guide_clear_button = self._card_button("✕", "Clear guide track")
        self.guide_clear_button.clicked.connect(self.guide_clear_requested.emit)
        self.guide_clear_button.setVisible(False)
        card, self.guide_file_label, self.guide_empty_button = \
            self._create_file_card(
                "guide", "🎧", "GUIDE", 'guide',
                "Load a full-mix guide track (design reference)",
                "+ Load guide", extras=(self.guide_clear_button,))
        cards_layout.addWidget(card, 1)

        layout.addLayout(cards_layout, 1)

        self.logger.debug("Song files section created")
        return section

    @staticmethod
    def _card_button(text: str, tooltip: str) -> QToolButton:
        """A small square header action for a SONG FILES card."""
        button = QToolButton()
        button.setText(text)
        button.setFixedSize(24, 22)
        button.setObjectName("FileCardButton")
        button.setToolTip(tooltip)
        return button

    def _create_file_card(self, kind: str, icon: str, title: str,
                          file_type: str, load_tooltip: str, empty_text: str,
                          extras: tuple = ()):
        """One SONG FILES card: icon + accent-coloured title + optional extra
        header actions + the 🗁 load button, over a path well / dashed
        empty-state action pair (one visible at a time; empty at build).
        Returns (card, path_label, empty_button); the accent rides the
        `kind` QSS dynamic property."""
        card = QFrame()
        card.setObjectName("FileCard")
        card.setProperty("kind", kind)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(8, 5, 8, 6)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.setSpacing(5)
        icon_label = QLabel(icon)
        icon_label.setObjectName("FileCardIcon")
        header.addWidget(icon_label)
        title_label = QLabel(title)
        title_label.setObjectName("FileCardTitle")
        title_label.setProperty("kind", kind)
        font = title_label.font()
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
        title_label.setFont(font)
        header.addWidget(title_label)
        header.addStretch(1)
        for extra in extras:
            header.addWidget(extra)
        load_button = self._card_button("🗁", load_tooltip)
        load_button.clicked.connect(
            lambda: self.load_file_requested.emit(file_type))
        header.addWidget(load_button)
        layout.addLayout(header)

        path_label = QLabel()
        path_label.setObjectName("FileCardPath")
        path_label.setFixedHeight(24)
        path_label.setMinimumWidth(60)
        path_label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                 QSizePolicy.Policy.Fixed)
        # Right-aligned so an overflowing path clips on the left and the
        # filename end stays visible.
        path_label.setAlignment(Qt.AlignmentFlag.AlignRight
                                | Qt.AlignmentFlag.AlignVCenter)
        path_label.setVisible(False)
        layout.addWidget(path_label)

        empty_button = QPushButton(empty_text)
        empty_button.setObjectName("FileCardAction")
        empty_button.setProperty("kind", kind)
        empty_button.setFixedHeight(24)
        empty_button.setToolTip(load_tooltip)
        empty_button.clicked.connect(
            lambda: self.load_file_requested.emit(file_type))
        layout.addWidget(empty_button)

        return card, path_label, empty_button

    def _on_combo_activated(self, index: int) -> None:
        """A dropdown pick (never programmatic repopulation) switches song."""
        if not self._populating_setlist:
            self.song_selected.emit(index)

    def _create_menu_section(self) -> QFrame:
        """Create the hamburger application menu section (far-right)."""
        section = QFrame()
        section.setFrameStyle(QFrame.Shape.StyledPanel)
        section.setObjectName("ToolbarSection")

        layout = QVBoxLayout(section)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(0)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        button = QToolButton()
        button.setText("☰")  # U+2630 TRIGRAM FOR HEAVEN — three horizontal bars
        button.setObjectName("MenuButton")
        button.setFixedSize(36, 36)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setToolTip("Menu")
        font = button.font()
        font.setPointSize(font.pointSize() + 4)
        button.setFont(font)

        menu = QMenu(button)
        settings_action = menu.addAction("Settings…")
        settings_action.triggered.connect(self.settings_requested.emit)
        about_action = menu.addAction("About TupperDMX")
        about_action.triggered.connect(self.about_requested.emit)
        button.setMenu(menu)

        layout.addWidget(button)

        self.menu_button = button
        self.logger.debug("Application menu section created")
        return section

    def _update_file_card(self, path_label: Optional[QLabel],
                          empty_button: Optional[QPushButton],
                          filename: str) -> None:
        """Swap a SONG FILES card between its path well (right-anchored so
        the filename end stays visible; full path in the tooltip) and its
        dashed empty-state load action."""
        if path_label is None:
            return
        path_label.setText(filename or "")
        path_label.setToolTip(filename or "")
        path_label.setVisible(bool(filename))
        if empty_button is not None:
            empty_button.setVisible(not filename)

    def update_audio_file_display(self, filename: str) -> None:
        """Update the BACKING TRACK card."""
        self._update_file_card(self.audio_file_label,
                               self.audio_empty_button, filename)
        self.logger.debug(f"Audio file display updated: {filename or '(empty)'}")

    def update_guide_file_display(self, filename: str) -> None:
        """Update the GUIDE card (empty = no guide; the ✕ clear action only
        shows while a guide is set)."""
        self._update_file_card(self.guide_file_label,
                               self.guide_empty_button, filename)
        if self.guide_clear_button:
            self.guide_clear_button.setVisible(bool(filename))
        self.logger.debug(f"Guide file display updated: {filename or '(empty)'}")

    def update_midi_file_display(self, filename: str) -> None:
        """Update the MIDI card."""
        self._update_file_card(self.midi_file_label,
                               self.midi_empty_button, filename)
        self.logger.debug(f"MIDI file display updated: {filename or '(empty)'}")

    def update_song_info(self, title: str, status: str,
                         bpm: Optional[int] = None,
                         offset: float = 0.0,
                         key: str = "") -> None:
        """Refresh the SETLIST footer for the current song: BPM, audio offset,
        the derived media chip ('linked' = audio and MIDI both present and
        resolvable) and the musical key. The title itself lives in the setlist
        combobox."""
        if self.bpm_label is not None:
            self.bpm_label.setText(f"{int(bpm)} BPM" if bpm else "")
        if self.offset_label is not None:
            self.offset_label.setText(
                f"offset {offset:+.2f}s" if title else "")
        if self.key_edit is not None:
            self.key_edit.blockSignals(True)
            self.key_edit.setText(key)
            self.key_edit.setEnabled(bool(title))
            self.key_edit.blockSignals(False)
        if self.linked_chip is None:
            return
        text, color = {
            "linked": ("🎵+🥁 linked", "#4caf50"),
            "audio_only": ("🎵 audio only", "#888888"),
            "no_audio": ("no audio", "#888888"),
            "missing_audio": ("⚠ audio missing", "#e24a4a"),
            "missing_midi": ("⚠ MIDI missing", "#e24a4a"),
        }.get(status, ("", ""))
        self.linked_chip.setText(text)
        self.linked_chip.setStyleSheet(f"color: {color};" if color else "")

    def set_song_bpm(self, bpm: int) -> None:
        """Live-update the SETLIST footer BPM (e.g. on manual tempo change)."""
        if self.bpm_label is not None:
            self.bpm_label.setText(f"{int(bpm)} BPM" if bpm else "")

    def set_setlist(self, titles: list, current_index: int) -> None:
        """Repopulate the song-selector combobox from the setlist and select
        the current song, without firing a selection signal."""
        if self.song_combo is None:
            return
        self._populating_setlist = True
        try:
            self.song_combo.clear()
            self.song_combo.addItems(
                [t or f"Song {i + 1}" for i, t in enumerate(titles)])
            if 0 <= current_index < len(titles):
                self.song_combo.setCurrentIndex(current_index)
            else:
                self.song_combo.setEditText("")
        finally:
            self._populating_setlist = False

    def set_export_midi_enabled(self, enabled: bool) -> None:
        """Enable or disable the export-MIDI menu action."""
        if self.export_midi_action:
            self.export_midi_action.setEnabled(enabled)
            self.logger.debug(f"Export MIDI button {'enabled' if enabled else 'disabled'}")

    def update_song_counter(self, current: int, total: int) -> None:
        """Update the setlist counter badge ('05/11') and header song count."""
        if self.setlist_counter_badge:
            self.setlist_counter_badge.setText(f"{current:02d}/{total:02d}")
        if self.setlist_count_label:
            self.setlist_count_label.setText(
                f"{total} song{'s' if total != 1 else ''}")

    def set_navigation_enabled(self, has_prev: bool, has_next: bool) -> None:
        """Enable/disable the prev/next navigation buttons."""
        if self.prev_button:
            self.prev_button.setEnabled(has_prev)
        if self.next_button:
            self.next_button.setEnabled(has_next)
        self.logger.debug(f"Navigation buttons: prev={has_prev}, next={has_next}")