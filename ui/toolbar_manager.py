from typing import Callable, Optional
from PyQt6.QtWidgets import (QToolBar, QLabel, QWidget, QSizePolicy,
                             QFrame, QHBoxLayout, QLineEdit, QPushButton,
                             QVBoxLayout, QToolButton, QMenu)
from PyQt6.QtGui import QAction, QFont
from PyQt6.QtCore import QObject, pyqtSignal, Qt

from tupperlogger import get_logger
from core.config import Config


class ToolbarManager(QObject):
    """Manages the main application toolbar with media controls and configuration options."""

    # Signals
    load_file_requested = pyqtSignal(str)  # file_type ('midi' or 'audio')
    new_midi_track_requested = pyqtSignal()  # request to create new empty MIDI track
    export_midi_requested = pyqtSignal()  # request to export MIDI file
    # Show project signals
    new_show_requested = pyqtSignal()
    open_show_requested = pyqtSignal()
    save_show_requested = pyqtSignal()
    save_show_as_requested = pyqtSignal()
    # Version signals
    snapshot_requested = pyqtSignal()
    history_requested = pyqtSignal()
    # Bundle / setlist signals
    export_bundle_requested = pyqtSignal()
    import_bundle_requested = pyqtSignal()
    song_title_edited = pyqtSignal(str)
    # Playlist navigation signals
    prev_song_requested = pyqtSignal()
    next_song_requested = pyqtSignal()
    remove_song_requested = pyqtSignal()
    # Application menu signals
    settings_requested = pyqtSignal()
    about_requested = pyqtSignal()

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.logger = get_logger("ui.toolbar")

        # Toolbar and widgets
        self.toolbar: Optional[QToolBar] = None
        self.export_midi_button: Optional[QPushButton] = None
        self.audio_file_label: Optional[QLabel] = None
        self.midi_file_label: Optional[QLabel] = None
        # Playlist navigation widgets
        self.prev_button: Optional[QPushButton] = None
        self.next_button: Optional[QPushButton] = None
        self.remove_button: Optional[QPushButton] = None
        self.song_counter_label: Optional[QLabel] = None
        self.menu_button: Optional[QToolButton] = None
        # Show section widgets
        self.show_name_label: Optional[QLabel] = None
        self.autosave_status_label: Optional[QLabel] = None
        # Setlist widgets
        self.song_title_edit = None
        self.linked_chip: Optional[QLabel] = None

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

        # Files Section (left - expandable to fill left space)
        files_section = self._create_files_section()
        container_layout.addWidget(files_section, 1)  # stretch factor 1 = expandable

        # Export Section (interop MIDI + portable bundle)
        export_section = self._create_export_section()
        container_layout.addWidget(export_section, 0)

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
        """Create the show-project section: New/Open/Save/Save as… plus the
        show name and the live autosave state."""
        section = QFrame()
        section.setFrameStyle(QFrame.Shape.StyledPanel)
        section.setObjectName("ToolbarSection")

        layout = QVBoxLayout(section)
        layout.setContentsMargins(10, 5, 10, 5)

        buttons_layout = QHBoxLayout()
        for text, tooltip, signal in (
                ("New", "Create a new show project", self.new_show_requested),
                ("Open", "Open a show folder", self.open_show_requested),
                ("Save", "Save the show now (autosave already runs)", self.save_show_requested),
                ("Save as…", "Duplicate the show under a new name", self.save_show_as_requested)):
            button = QPushButton(text)
            button.setFixedHeight(30)
            button.setObjectName("LoadButton")
            button.setToolTip(tooltip)
            button.clicked.connect(signal.emit)
            buttons_layout.addWidget(button)
        layout.addLayout(buttons_layout)

        info_layout = QHBoxLayout()
        info_layout.setSpacing(8)
        self.show_name_label = QLabel("No show")
        self.show_name_label.setFixedHeight(20)
        self.show_name_label.setObjectName("FileLabel")
        info_layout.addWidget(self.show_name_label)
        self.autosave_status_label = QLabel("")
        self.autosave_status_label.setFixedHeight(20)
        self.autosave_status_label.setObjectName("FileLabel")
        info_layout.addWidget(self.autosave_status_label)
        info_layout.addStretch(1)
        layout.addLayout(info_layout)

        self.logger.debug("Show section created")
        return section

    def _create_export_section(self) -> QFrame:
        """Create the export section: interop MIDI copy + portable bundle."""
        section = QFrame()
        section.setFrameStyle(QFrame.Shape.StyledPanel)
        section.setObjectName("ToolbarSection")

        layout = QVBoxLayout(section)
        layout.setContentsMargins(10, 5, 10, 5)

        buttons_layout = QHBoxLayout()
        self.export_midi_button = QPushButton("Export MIDI…")
        self.export_midi_button.setFixedHeight(30)
        self.export_midi_button.setObjectName("ExportButton")
        self.export_midi_button.setToolTip(
            "Standard-MIDI interchange copy of the current song (the show "
            "itself autosaves)")
        self.export_midi_button.clicked.connect(self.export_midi_requested.emit)
        self.export_midi_button.setEnabled(False)  # Disabled by default
        buttons_layout.addWidget(self.export_midi_button)

        export_bundle_btn = QPushButton("Export bundle…")
        export_bundle_btn.setFixedHeight(30)
        export_bundle_btn.setObjectName("ExportButton")
        export_bundle_btn.setToolTip(
            "One portable .showpack with audio, MIDI, patch and fixtures")
        export_bundle_btn.clicked.connect(self.export_bundle_requested.emit)
        buttons_layout.addWidget(export_bundle_btn)
        layout.addLayout(buttons_layout)

        label = QLabel("EXPORT · portable .showpack, all songs")
        label.setFixedHeight(20)
        label.setObjectName("FileLabel")
        layout.addWidget(label)

        self.logger.debug("Export section created")
        return section

    def _create_versions_section(self) -> QFrame:
        """Create the versions section: manual pinned snapshot + history."""
        section = QFrame()
        section.setFrameStyle(QFrame.Shape.StyledPanel)
        section.setObjectName("ToolbarSection")

        layout = QVBoxLayout(section)
        layout.setContentsMargins(10, 5, 10, 5)

        buttons_layout = QHBoxLayout()
        for text, tooltip, signal in (
                ("📌 Snapshot…", "Take a named, never-pruned snapshot",
                 self.snapshot_requested),
                ("History…", "Browse and restore show snapshots",
                 self.history_requested)):
            button = QPushButton(text)
            button.setFixedHeight(30)
            button.setObjectName("LoadButton")
            button.setToolTip(tooltip)
            button.clicked.connect(signal.emit)
            buttons_layout.addWidget(button)
        layout.addLayout(buttons_layout)

        label = QLabel("VERSIONS")
        label.setFixedHeight(20)
        label.setObjectName("FileLabel")
        layout.addWidget(label)

        self.logger.debug("Versions section created")
        return section

    def set_show_name(self, name: str) -> None:
        """Update the show-name display in the SHOW section."""
        if self.show_name_label:
            self.show_name_label.setText(f"{name}.show" if name else "No show")

    def set_autosave_state(self, state: str) -> None:
        """Reflect the ShowManager autosave state ('saved'|'pending'|'error')."""
        if not self.autosave_status_label:
            return
        text, color = {
            "saved": ("● All changes saved", "#4caf50"),
            "pending": ("● Editing · autosave armed", "#e2a04a"),
            "error": ("⚠ Save error — check logs", "#e24a4a"),
        }.get(state, ("", ""))
        self.autosave_status_label.setText(text)
        self.autosave_status_label.setStyleSheet(f"color: {color};" if color else "")

    def _create_files_section(self) -> QFrame:
        """Create the files loading section."""
        section = QFrame()
        section.setFrameStyle(QFrame.Shape.StyledPanel)
        section.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        section.setObjectName("ToolbarSection")
        
        layout = QVBoxLayout(section)
        layout.setContentsMargins(10, 5, 10, 5)
        
        # File loading buttons
        buttons_layout = QHBoxLayout()
        
        # Playlist navigation buttons
        self.prev_button = QPushButton("⏮")
        self.prev_button.setFixedSize(30, 30)
        self.prev_button.setObjectName("NavButton")
        self.prev_button.setToolTip("Previous song")
        self.prev_button.clicked.connect(self.prev_song_requested.emit)
        self.prev_button.setEnabled(False)
        buttons_layout.addWidget(self.prev_button)
        
        # Song counter label
        self.song_counter_label = QLabel("0/0")
        self.song_counter_label.setFixedSize(50, 30)
        self.song_counter_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.song_counter_label.setObjectName("SongCounter")
        self.song_counter_label.setToolTip("Current song / Total songs")
        buttons_layout.addWidget(self.song_counter_label)
        
        self.next_button = QPushButton("⏭")
        self.next_button.setFixedSize(30, 30)
        self.next_button.setObjectName("NavButton")
        self.next_button.setToolTip("Next song")
        self.next_button.clicked.connect(self.next_song_requested.emit)
        self.next_button.setEnabled(False)
        buttons_layout.addWidget(self.next_button)
        
        self.remove_button = QPushButton("🗑")
        self.remove_button.setFixedSize(30, 30)
        self.remove_button.setObjectName("RemoveButton")
        self.remove_button.setToolTip("Remove current song from playlist")
        self.remove_button.clicked.connect(self.remove_song_requested.emit)
        buttons_layout.addWidget(self.remove_button)
        
        buttons_layout.addSpacing(10)
        
        # Load MIDI button
        load_midi_btn = QPushButton("Load MIDI file")
        load_midi_btn.setMinimumWidth(130)
        load_midi_btn.setFixedHeight(30)
        load_midi_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        load_midi_btn.setObjectName("LoadButton")
        load_midi_btn.clicked.connect(lambda: self.load_file_requested.emit('midi'))
        buttons_layout.addWidget(load_midi_btn)

        # Load Audio button
        load_audio_btn = QPushButton("Load Audio file")
        load_audio_btn.setMinimumWidth(140)
        load_audio_btn.setFixedHeight(30)
        load_audio_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        load_audio_btn.setObjectName("LoadButton")
        load_audio_btn.clicked.connect(lambda: self.load_file_requested.emit('audio'))
        buttons_layout.addWidget(load_audio_btn)

        # New MIDI track button
        new_midi_btn = QPushButton("New MIDI track")
        new_midi_btn.setMinimumWidth(140)
        new_midi_btn.setFixedHeight(30)
        new_midi_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        new_midi_btn.setObjectName("NewButton")
        new_midi_btn.clicked.connect(self.new_midi_track_requested.emit)
        buttons_layout.addWidget(new_midi_btn)

        layout.addLayout(buttons_layout)

        # Setlist row: editable song title + derived audio/MIDI "linked" chip.
        title_layout = QHBoxLayout()
        self.song_title_edit = QLineEdit()
        self.song_title_edit.setPlaceholderText("Song title")
        self.song_title_edit.setFixedHeight(24)
        self.song_title_edit.editingFinished.connect(
            lambda: self.song_title_edited.emit(self.song_title_edit.text()))
        title_layout.addWidget(self.song_title_edit, 1)
        self.linked_chip = QLabel("")
        self.linked_chip.setFixedHeight(24)
        self.linked_chip.setObjectName("FileLabel")
        title_layout.addWidget(self.linked_chip, 0)
        layout.addLayout(title_layout)
        
        # File path labels container
        file_paths_layout = QVBoxLayout()
        file_paths_layout.setSpacing(2)
        
        # Audio file info (full width to show complete path)
        self.audio_file_label = QLabel("No audio file")
        self.audio_file_label.setFixedHeight(20)
        self.audio_file_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.audio_file_label.setWordWrap(False)  # Disable word wrapping
        self.audio_file_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.audio_file_label.setObjectName("FileLabel")
        file_paths_layout.addWidget(self.audio_file_label)
        
        # MIDI file info (full width to show complete path)
        self.midi_file_label = QLabel("No MIDI file")
        self.midi_file_label.setFixedHeight(20)
        self.midi_file_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.midi_file_label.setWordWrap(False)  # Disable word wrapping
        self.midi_file_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.midi_file_label.setObjectName("FileLabel")
        file_paths_layout.addWidget(self.midi_file_label)
        
        layout.addLayout(file_paths_layout)
        
        self.logger.debug("Files section created")
        return section

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
        import_action = menu.addAction("Import show bundle…")
        import_action.triggered.connect(self.import_bundle_requested.emit)
        menu.addSeparator()
        settings_action = menu.addAction("Settings…")
        settings_action.triggered.connect(self.settings_requested.emit)
        about_action = menu.addAction("About TupperDMX")
        about_action.triggered.connect(self.about_requested.emit)
        button.setMenu(menu)

        layout.addWidget(button)

        self.menu_button = button
        self.logger.debug("Application menu section created")
        return section

    def update_audio_file_display(self, filename: str) -> None:
        """Update the audio file display."""
        if hasattr(self, 'audio_file_label') and self.audio_file_label:
            display_name = filename if filename else "No audio file"
            # No truncation needed - let the label expand to show full path
            self.audio_file_label.setText(display_name)
            # Set tooltip to show full path if it's long
            if filename and len(filename) > 50:
                self.audio_file_label.setToolTip(filename)
            else:
                self.audio_file_label.setToolTip("")
            self.logger.debug(f"Audio file display updated: {display_name}")
            
    def update_midi_file_display(self, filename: str) -> None:
        """Update the MIDI file display."""
        if hasattr(self, 'midi_file_label') and self.midi_file_label:
            if filename:
                display_name = filename
            else:
                display_name = "No MIDI file"
            # No truncation needed - let the label expand to show full path
            self.midi_file_label.setText(display_name)
            # Set tooltip to show full path if it's long
            if filename and len(filename) > 50:
                self.midi_file_label.setToolTip(filename)
            else:
                self.midi_file_label.setToolTip("")
            self.logger.debug(f"MIDI file display updated: {display_name}")

    def update_song_info(self, title: str, status: str) -> None:
        """Refresh the setlist row: editable title + the derived media chip
        ('linked' = audio and MIDI both present and resolvable)."""
        if self.song_title_edit is not None and not self.song_title_edit.hasFocus():
            self.song_title_edit.setText(title or "")
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

    def set_export_midi_enabled(self, enabled: bool) -> None:
        """Enable or disable the export MIDI button."""
        if self.export_midi_button:
            self.export_midi_button.setEnabled(enabled)
            self.logger.debug(f"Export MIDI button {'enabled' if enabled else 'disabled'}")

    def update_song_counter(self, current: int, total: int) -> None:
        """Update the song counter display (e.g., '2/5')."""
        if self.song_counter_label:
            self.song_counter_label.setText(f"{current}/{total}")
            self.logger.debug(f"Song counter updated: {current}/{total}")
    
    def set_navigation_enabled(self, has_prev: bool, has_next: bool) -> None:
        """Enable/disable the prev/next navigation buttons."""
        if self.prev_button:
            self.prev_button.setEnabled(has_prev)
        if self.next_button:
            self.next_button.setEnabled(has_next)
        self.logger.debug(f"Navigation buttons: prev={has_prev}, next={has_next}")