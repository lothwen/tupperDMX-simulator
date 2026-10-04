import html
from datetime import datetime
from typing import Callable, Optional
from PyQt6.QtWidgets import (QToolBar, QLabel, QWidget, QSizePolicy,
                             QFrame, QHBoxLayout, QPushButton, QLineEdit,
                             QVBoxLayout, QToolButton, QMenu, QComboBox)
from PyQt6.QtGui import QAction, QFont, QIcon
from PyQt6.QtCore import QObject, pyqtSignal, Qt, QSize

from ui.widgets import icons
from tupperlogger import get_logger
from core.config import Config
from core.paths import asset_path
from webremote.state import probe_duration


class _ResponsiveBar(QWidget):
    """The toolbar's content widget, which reports the width of its *compact*
    layout rather than its full one.

    A widget cannot react to being narrower than its own minimum — Qt never
    lets it get there, so a plain resizeEvent breakpoint would never fire. By
    advertising the folded minimum the window is free to shrink, the resize
    arrives, and the manager folds the low-priority sections away for real.
    """

    def __init__(self, on_resize: Callable[[int], None]):
        super().__init__()
        self._on_resize = on_resize
        self._compact_min = 0

    def set_compact_minimum(self, width: int) -> None:
        self._compact_min = width
        self.updateGeometry()

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        if self._compact_min:
            return QSize(self._compact_min, hint.height())
        return hint

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._on_resize(event.size().width())


class _StatusChip(QLabel):
    """A status label that takes no width at all until it has something to say.

    Qt sizes an empty QLabel to `fontMetrics().averageCharWidth()`, which is a
    sane ~7px for the theme's "Noto Sans" and pure nonsense for whatever a box
    without it substitutes — on CI that came back as 2550px. Five empty chips
    then put the bar 10000px over its folded minimum, and a minimum that large
    is one no window can satisfy, so the bar could never fold.
    """

    def _blank(self, hint: QSize) -> QSize:
        return QSize(0, hint.height()) if not self.text() else hint

    def sizeHint(self) -> QSize:
        return self._blank(super().sizeHint())

    def minimumSizeHint(self) -> QSize:
        return self._blank(super().minimumSizeHint())


class ToolbarManager(QObject):
    """Manages the main application toolbar with media controls and configuration options."""

    # Signals
    load_file_requested = pyqtSignal(str)  # file_type ('midi', 'audio' or 'guide')
    audio_clear_requested = pyqtSignal()  # remove the current song's backing track
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
    song_key_edited = pyqtSignal(str)        # current song's musical key (TONO)
    # Playlist navigation signals
    prev_song_requested = pyqtSignal()
    next_song_requested = pyqtSignal()
    new_song_requested = pyqtSignal()     # append an empty song to the setlist
    playlist_requested = pyqtSignal()     # open the playlist manager
    auto_program_requested = pyqtSignal()  # open the auto program
    song_selected = pyqtSignal(int)  # setlist combobox picked a song by index
    # Application menu signals
    settings_requested = pyqtSignal()
    about_requested = pyqtSignal()
    dock_stage_requested = pyqtSignal()  # bring the undocked stage window back

    # Breathing room over the bar's own minimum before the cards come back, so
    # the expanded layout is never restored into a width it barely fits.
    BREAKPOINT_SLACK = 24

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.logger = get_logger("ui.toolbar")

        # Toolbar and widgets
        self.toolbar: Optional[QToolBar] = None
        self.container: Optional[QWidget] = None
        self.export_midi_menu_action: Optional[QAction] = None
        # Narrow-window state: the SHOW card that folds into the ☰ menu, the
        # width that decides it, and the menu actions that stand in for it.
        self.show_section: Optional[QFrame] = None
        self._compact = False
        self._full_min_width = 0
        self._compact_actions: list = []
        self.versions_menu: Optional[QMenu] = None
        self.export_midi_action: Optional[QAction] = None
        # Song-files cards (path wells + empty-state load actions)
        self.audio_file_label: Optional[QLabel] = None
        self.audio_empty_button: Optional[QPushButton] = None
        self.audio_clear_button: Optional[QToolButton] = None
        self.guide_file_label: Optional[QLabel] = None
        self.guide_empty_button: Optional[QPushButton] = None
        self.guide_clear_button: Optional[QToolButton] = None
        self.midi_file_label: Optional[QLabel] = None
        self.midi_empty_button: Optional[QPushButton] = None
        # Playlist navigation widgets
        self.prev_button: Optional[QPushButton] = None
        self.next_button: Optional[QPushButton] = None
        self.new_song_button: Optional[QPushButton] = None
        self.playlist_button: Optional[QPushButton] = None
        self.menu_button: Optional[QToolButton] = None
        self.dock_stage_action = None
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

        self.logger.info("Toolbar manager initialized")
    
    def create_toolbar(self, parent) -> QToolBar:
        """Create and setup the main toolbar with grouped sections."""
        self.toolbar = QToolBar("Main Toolbar", parent)
        self.toolbar.setMinimumHeight(115)
        self.toolbar.setMovable(False)
        self.toolbar.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        
        # Create a container widget with horizontal layout to properly manage expansion
        container = _ResponsiveBar(self._on_bar_resized)
        container_layout = QHBoxLayout(container)
        container_layout.setContentsMargins(10, 10, 10, 10)
        container_layout.setSpacing(15)
        
        # Show Section (far left - fixed width project controls)
        self.show_section = self._create_show_section()
        container_layout.addWidget(self.show_section, 0)

        # Setlist Section (playlist navigation + song selector)
        setlist_section = self._create_setlist_section()
        container_layout.addWidget(setlist_section, 2)

        # Song Files Section (per-song audio/MIDI loading + info)
        song_files_section = self._create_song_files_section()
        container_layout.addWidget(song_files_section, 3)  # widest — long paths

        # Menu Section (far right - fixed width hamburger button)
        menu_section = self._create_menu_section()
        container_layout.addWidget(menu_section, 0)

        self.container = container
        self._measure_breakpoint()

        # Add the container to the toolbar
        self.toolbar.addWidget(container)
        
        self.logger.info("Main toolbar created")
        return self.toolbar

    # ------------------------------------------------------------------
    # Narrow windows: SHOW folds into the ☰ menu
    # ------------------------------------------------------------------
    def _measure_breakpoint(self) -> None:
        """Record what the bar needs with everything shown and with SHOW
        folded away. Measured rather than hardcoded so the numbers follow the
        UI density and any later change to the sections.

        The two widths are derived from whichever state the bar is in plus what
        the folding section costs, rather than by hiding and showing it to
        measure: `minimumSizeHint()` is valid on a hidden widget, while a
        section shown and re-measured in one call stack still reports the size
        it had before Qt laid it out again.
        """
        container = self.container
        container.ensurePolished()
        layout = container.layout()
        layout.invalidate()
        layout.activate()

        # A hidden widget takes neither width nor a spacing gap, so folding
        # the section saves its minimum and its gap.
        folded = self.show_section.minimumSizeHint().width() + layout.spacing()
        current = layout.minimumSize().width()
        if self._compact:
            compact_min, self._full_min_width = current, current + folded
        else:
            compact_min, self._full_min_width = current - folded, current
        container.set_compact_minimum(compact_min)

        self.logger.debug("Toolbar widths: full %dpx, compact %dpx",
                          self._full_min_width, compact_min)

    def remeasure_breakpoint(self) -> None:
        """Re-measure after a UI density change and re-decide from the width
        the bar actually has (a resize event may never come).

        Folded, the sections are hidden, and Qt does not restyle a hidden
        widget — so a density change read here lands a few tens of pixels wide
        of the truth until the next launch measures it expanded. It errs
        towards staying folded, which costs nothing.
        """
        if self.container is None:
            return
        self._measure_breakpoint()
        self._on_bar_resized(self.container.width())

    def _on_bar_resized(self, width: int) -> None:
        """Fold the SHOW card away when the full bar would not fit.

        Hysteresis is implicit: it collapses only when the expanded layout is
        too wide for `width` and expands only when it would fit again, so the
        two decisions can never chase each other.
        """
        if not self._full_min_width:
            return
        if self._compact:
            if width >= self._full_min_width + self.BREAKPOINT_SLACK:
                self.set_compact(False)
        elif width < self._full_min_width + self.BREAKPOINT_SLACK:
            self.set_compact(True)

    def set_compact(self, compact: bool) -> None:
        """Show or fold the SHOW card. Folded, its actions are reachable from
        the ☰ menu instead — the same signals either way."""
        if compact == self._compact:
            return
        self._compact = compact
        self.show_section.setVisible(not compact)
        for action in self._compact_actions:
            action.setVisible(compact)
        self.logger.debug("Toolbar %s", "folded" if compact else "expanded")
    
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
        for icon_name, text, tooltip, signal in (
                ("plus", "New", "Create a new show project", self.new_show_requested),
                ("folder-open", "Open", "Open a show folder", self.open_show_requested),
                ("save", "Save", "Save the show now (autosave already runs)", self.save_show_requested),
                (None, "Save as…", "Duplicate the show under a new name", self.save_show_as_requested)):
            button = QPushButton(text)
            if icon_name:
                button.setIcon(icons.icon(icon_name, "#e6e6e6"))
                button.setIconSize(QSize(14, 14))
            button.setFixedHeight(30)
            button.setObjectName("LoadButton")
            button.setToolTip(tooltip)
            button.clicked.connect(signal.emit)
            buttons_layout.addWidget(button)
        more_button = QToolButton()
        more_button.setIcon(icons.icon("more", "#e6e6e6"))
        more_button.setIconSize(QSize(16, 16))
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
        export_button.setText("Export")
        export_button.setIcon(icons.icon("download", "#eaf4fb"))
        export_button.setIconSize(QSize(14, 14))
        export_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
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
        self.export_midi_action = export_menu.addAction(
            icons.icon("download"), "Export MIDI…")
        self.export_midi_action.setToolTip(
            "Standard-MIDI interchange copy of the current song (the show "
            "itself autosaves)")
        self.export_midi_action.triggered.connect(self.export_midi_requested.emit)
        self.export_midi_action.setEnabled(False)  # Disabled by default
        bundle_action = export_menu.addAction(
            icons.icon("package"), "Export bundle")
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
        self.show_stats_label = _StatusChip("")
        self.show_stats_label.setObjectName("ShowStatsLabel")
        self.show_stats_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        info_layout.addWidget(self.show_stats_label)
        layout.addLayout(info_layout)
        self.update_show_stats()

        self.logger.debug("Show section created")
        return section

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
        """Populate the ☰ menu's Restore submenu, newest first, from a list of
        `versions.VersionInfo`. A show with no history yet leaves the submenu
        there but disabled — there is nothing to roll back to."""
        if self.versions_menu is None:
            return
        self.versions_menu.clear()
        self.versions_menu.setEnabled(bool(infos))
        for info in infos:
            action = self.versions_menu.addAction(
                self._version_icon(info), self._version_label(info))
            action.triggered.connect(
                lambda _checked=False, version_id=info.version_id:
                self.restore_version_requested.emit(version_id))

    @staticmethod
    def _version_icon(info):
        """Menu icon for a snapshot: amber pin when pinned, dim save glyph
        for autosaves."""
        if info.pinned:
            return icons.icon("pin", "#e2a04a")
        return icons.icon("save", "#8a8a8a")

    def _version_label(self, info) -> str:
        """Menu label for a snapshot: 'name · time' when pinned, else
        'Autosave · time' (the icon carries the pinned/auto distinction)."""
        when = self._format_version_time(info.timestamp)
        if info.pinned:
            return f"{info.name} · {when}" if info.name else when
        return f"Autosave · {when}"

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
            "error": (f'{icons.img_tag("warning", "#e24a4a", 11)} '
                      "Save error — check logs", "#e24a4a"),
        }.get(state, ("", ""))
        self.autosave_status_label.setText(text)
        self.autosave_status_label.setStyleSheet(f"color: {color};" if color else "")

    def _create_setlist_section(self) -> QFrame:
        """Create the SETLIST section: header (title + song count), a row with
        prev / song selector combobox / next / new / remove, and a footer with
        the current song's BPM, offset and derived media 'linked' chip."""
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

        self.prev_button = QPushButton()
        self.prev_button.setIcon(icons.icon("prev", "#e6e6e6"))
        self.prev_button.setIconSize(QSize(15, 15))
        self.prev_button.setFixedSize(35, 28)
        self.prev_button.setObjectName("NavButton")
        self.prev_button.setToolTip("Previous song")
        self.prev_button.clicked.connect(self.prev_song_requested.emit)
        self.prev_button.setEnabled(False)
        controls_layout.addWidget(self.prev_button)

        # Unified selector: a counter badge + combobox in one rounded frame
        # (the frame carries the border; the combobox is drawn flat).
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
        self.song_combo.setPlaceholderText("No songs")
        self.song_combo.setToolTip("Select a song (rename it in the playlist manager)")
        self.song_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        # A title the operator can still read is the point of this control, so
        # give it a floor of its own rather than letting the row squeeze it.
        self.song_combo.setMinimumContentsLength(12)
        # Selecting from the dropdown switches song. activated() only fires on
        # user action (not programmatic repopulation), so no guard is needed.
        self.song_combo.activated.connect(self._on_combo_activated)
        selector_layout.addWidget(self.song_combo, 1)
        controls_layout.addWidget(selector, 1)

        self.next_button = QPushButton()
        self.next_button.setIcon(icons.icon("next", "#e6e6e6"))
        self.next_button.setIconSize(QSize(15, 15))
        self.next_button.setFixedSize(35, 28)
        self.next_button.setObjectName("NavButton")
        self.next_button.setToolTip("Next song")
        self.next_button.clicked.connect(self.next_song_requested.emit)
        self.next_button.setEnabled(False)
        controls_layout.addWidget(self.next_button)

        # New song: an empty song to fill with whatever tracks the user has
        # (loading a file only ever edits the CURRENT song, so this is the one
        # way the setlist grows).
        self.new_song_button = QPushButton()
        self.new_song_button.setIcon(icons.icon("plus", "#e6e6e6"))
        self.new_song_button.setIconSize(QSize(15, 15))
        self.new_song_button.setFixedSize(28, 30)
        self.new_song_button.setObjectName("NavButton")
        self.new_song_button.setToolTip("Add a new empty song to the setlist")
        self.new_song_button.clicked.connect(self.new_song_requested.emit)
        controls_layout.addWidget(self.new_song_button)

        # The playlist manager: reorder, remove, import and edit every song
        # (the rest of this panel only ever touches the current one).
        self.playlist_button = QPushButton()
        self.playlist_button.setIcon(icons.icon("playlist", "#e6e6e6"))
        self.playlist_button.setIconSize(QSize(15, 15))
        self.playlist_button.setFixedSize(28, 30)
        self.playlist_button.setObjectName("NavButton")
        self.playlist_button.setToolTip("Manage playlist…")
        self.playlist_button.clicked.connect(self.playlist_requested.emit)
        controls_layout.addWidget(self.playlist_button)

        layout.addLayout(controls_layout)

        footer_layout = QHBoxLayout()
        footer_layout.setSpacing(10)
        self.bpm_label = _StatusChip("")
        self.bpm_label.setObjectName("BpmLabel")
        footer_layout.addWidget(self.bpm_label)
        self.offset_label = _StatusChip("")
        self.offset_label.setObjectName("OffsetLabel")
        footer_layout.addWidget(self.offset_label)
        self.linked_chip = _StatusChip("")
        self.linked_chip.setObjectName("LinkedChip")
        footer_layout.addWidget(self.linked_chip)
        footer_layout.addStretch(1)
        # Per-song live-player field (Fase 4): musical key. (The count-in
        # toggle lives on the timeline toolbar, next to the BPM spinbox.)
        self.key_edit = QLineEdit()
        self.key_edit.setObjectName("SongKeyEdit")
        self.key_edit.setPlaceholderText("key")
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
        self.autosave_status_label = _StatusChip("")
        self.autosave_status_label.setObjectName("AutosaveStatus")
        header_layout.addWidget(self.autosave_status_label)
        layout.addLayout(header_layout)

        cards_layout = QHBoxLayout()
        cards_layout.setSpacing(8)

        # MIDI card (+ the new-track header action)
        new_track_button = self._card_button(
            "plus", "Start a new empty MIDI track for this song")
        new_track_button.clicked.connect(self.new_midi_track_requested.emit)
        card, self.midi_file_label, self.midi_empty_button = \
            self._create_file_card(
                "midi", "piano", "MIDI", 'midi',
                "Load a MIDI file for this song", "+ Load MIDI",
                extras=(new_track_button,))
        cards_layout.addWidget(card, 1)

        # BACKING TRACK card (+ the ✕ clear header action, shown while
        # loaded) — the live master (web remote, headless). Clearing it leaves
        # the song standing: it keeps its guide, MIDI and cues, and the
        # SETLIST panel is where a song is removed.
        self.audio_clear_button = self._card_button("close", "Clear backing track")
        self.audio_clear_button.clicked.connect(self.audio_clear_requested.emit)
        self.audio_clear_button.setVisible(False)
        card, self.audio_file_label, self.audio_empty_button = \
            self._create_file_card(
                "backing", "music", "BACKING TRACK", 'audio',
                "Load the backing track for this song", "+ Load backing",
                extras=(self.audio_clear_button,))
        cards_layout.addWidget(card, 1)

        # GUIDE card (+ the ✕ clear header action, shown while loaded) —
        # the optional full-mix design reference.
        self.guide_clear_button = self._card_button("close", "Clear guide track")
        self.guide_clear_button.clicked.connect(self.guide_clear_requested.emit)
        self.guide_clear_button.setVisible(False)
        card, self.guide_file_label, self.guide_empty_button = \
            self._create_file_card(
                "guide", "headphones", "GUIDE", 'guide',
                "Load a full-mix guide track (design reference)",
                "+ Load guide", extras=(self.guide_clear_button,))
        cards_layout.addWidget(card, 1)

        layout.addLayout(cards_layout, 1)

        self.logger.debug("Song files section created")
        return section

    @staticmethod
    def _card_button(icon_name: str, tooltip: str) -> QToolButton:
        """A small square header action for a SONG FILES card."""
        button = QToolButton()
        button.setIcon(icons.icon(icon_name, "#b5b5b5"))
        button.setIconSize(QSize(12, 12))
        button.setFixedSize(24, 22)
        button.setObjectName("FileCardButton")
        button.setToolTip(tooltip)
        return button

    # Card accent colours, mirroring the FileCardTitle[kind=…] QSS rules.
    _CARD_ACCENTS = {"midi": "#c9c356", "backing": "#6fce7f",
                     "guide": "#5da9e8"}

    def _create_file_card(self, kind: str, icon_name: str, title: str,
                          file_type: str, load_tooltip: str, empty_text: str,
                          extras: tuple = ()):
        """One SONG FILES card: icon + accent-coloured title + optional extra
        header actions + the load button, over a path well / dashed
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
        icon_label = QLabel()
        icon_label.setPixmap(icons.pixmap(
            icon_name, 13, self._CARD_ACCENTS.get(kind, icons.DEFAULT_COLOR)))
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
        load_button = self._card_button("folder-open", load_tooltip)
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

    def _on_combo_activated(self, row: int) -> None:
        """A dropdown pick (never programmatic repopulation) switches song —
        by the song's playlist index, which the row carries: with disabled
        songs left out, the two differ."""
        if not self._populating_setlist:
            self.song_selected.emit(self.song_combo.itemData(row))

    def _add_compact_actions(self, menu: QMenu) -> None:
        """The SHOW card as menu entries, hidden until the bar actually folds.
        They emit the very same signals the buttons do, so a narrow window
        loses the card and nothing else."""
        for icon_name, text, signal in (
                ("plus", "New show…", self.new_show_requested),
                ("folder-open", "Open show…", self.open_show_requested),
                ("save", "Save show", self.save_show_requested),
                ("save", "Save show as…", self.save_show_as_requested),
                ("upload", "Import bundle…", self.import_bundle_requested),
                ("package", "Export bundle…", self.export_bundle_requested)):
            action = menu.addAction(icons.icon(icon_name), text)
            action.triggered.connect(signal.emit)
            self._compact_actions.append(action)

        # The MIDI export follows the split button's enabled state, which
        # set_export_midi_enabled() drives for both.
        self.export_midi_menu_action = menu.addAction(
            icons.icon("download"), "Export MIDI…")
        self.export_midi_menu_action.setEnabled(False)
        self.export_midi_menu_action.triggered.connect(
            self.export_midi_requested.emit)
        self._compact_actions.append(self.export_midi_menu_action)

        self._compact_actions.append(menu.addSeparator())
        for action in self._compact_actions:
            action.setVisible(False)

    def _add_version_actions(self, menu: QMenu) -> None:
        """The show's history: take a pinned snapshot, or roll back to one.
        The ☰ menu is the only home these have — there is no VERSIONS card —
        so they stay visible whatever width the bar is at."""
        snapshot_action = menu.addAction(icons.icon("pin"), "Snapshot")
        snapshot_action.setToolTip("Take a named, never-pruned snapshot")
        snapshot_action.triggered.connect(self.snapshot_requested.emit)

        # Filled by set_versions(); disabled until the show has a history.
        self.versions_menu = QMenu("Restore version", menu)
        self.versions_menu.setIcon(icons.icon("swap"))
        self.versions_menu.setEnabled(False)
        menu.addMenu(self.versions_menu)
        menu.addSeparator()

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
        button.setIcon(icons.icon("menu", "#e6e6e6"))
        button.setIconSize(QSize(20, 20))
        button.setObjectName("MenuButton")
        button.setFixedSize(36, 36)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setToolTip("Menu")

        menu = QMenu(button)
        menu.setToolTipsVisible(True)
        self._add_compact_actions(menu)
        auto_action = menu.addAction(icons.icon("bolt"), "Auto program…")
        auto_action.setToolTip("Generate a song's light show from its audio")
        auto_action.triggered.connect(self.auto_program_requested.emit)
        menu.addSeparator()
        self._add_version_actions(menu)
        # The stage's own dock button leaves with the stage, so this is the way
        # back when its window ends up on a display that is no longer there.
        self.dock_stage_action = menu.addAction(
            icons.icon("dock"), "Dock stage")
        self.dock_stage_action.setEnabled(False)
        self.dock_stage_action.triggered.connect(self.dock_stage_requested.emit)
        menu.addSeparator()
        settings_action = menu.addAction(icons.icon("settings"), "Settings…")
        settings_action.triggered.connect(self.settings_requested.emit)
        about_action = menu.addAction(
            QIcon(asset_path("icons", "TupperDMX-icon.png")), "About TupperDMX")
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
        """Update the BACKING TRACK card (empty = no backing; the ✕ clear
        action only shows while one is set — including a missing one, which
        is exactly when you may want it gone)."""
        self._update_file_card(self.audio_file_label,
                               self.audio_empty_button, filename)
        if self.audio_clear_button:
            self.audio_clear_button.setVisible(bool(filename))
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

    def update_offset_display(self, offset: float,
                             has_song: bool = True) -> None:
        """Refresh just the SETLIST footer's offset chip.

        Its own setter because the audio offset changes WITHOUT the song
        changing — a waveform drag retimes the audio under a song that is
        already loaded, and update_song_info's other arguments (title, status,
        bpm, key) are not the drag's to supply. Always the backing offset: the
        guide has its own, and the chip has always meant `song.offset`."""
        if self.offset_label is not None:
            self.offset_label.setText(
                f"offset {offset:+.2f}s" if has_song else "")

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
        self.update_offset_display(offset, has_song=bool(title))
        if self.key_edit is not None:
            self.key_edit.blockSignals(True)
            self.key_edit.setText(key)
            self.key_edit.setEnabled(bool(title))
            self.key_edit.blockSignals(False)
        if self.linked_chip is None:
            return
        text, color = {
            "linked": (f'{icons.img_tag("music", "#4caf50", 11)}+'
                       f'{icons.img_tag("piano", "#4caf50", 11)} linked',
                       "#4caf50"),
            "audio_only": (f'{icons.img_tag("music", "#888888", 11)} '
                           "audio only", "#888888"),
            "no_audio": ("no audio", "#888888"),
            "missing_audio": (f'{icons.img_tag("warning", "#e24a4a", 11)} '
                              "audio missing", "#e24a4a"),
            "missing_midi": (f'{icons.img_tag("warning", "#e24a4a", 11)} '
                             "MIDI missing", "#e24a4a"),
        }.get(status, ("", ""))
        self.linked_chip.setText(text)
        self.linked_chip.setStyleSheet(f"color: {color};" if color else "")

    def set_song_bpm(self, bpm: int) -> None:
        """Live-update the SETLIST footer BPM (e.g. on manual tempo change)."""
        if self.bpm_label is not None:
            self.bpm_label.setText(f"{int(bpm)} BPM" if bpm else "")

    def set_setlist(self, titles: list, current_index: int,
                    enabled: Optional[list] = None) -> None:
        """Repopulate the song-selector combobox from the setlist and select
        the current song, without firing a selection signal.

        A disabled song (`enabled[i]` false) is left out — it is out of the
        running order — unless it is the current one, which the selector
        still has to show."""
        if self.song_combo is None:
            return
        self._populating_setlist = True
        try:
            self.song_combo.clear()
            for i, title in enumerate(titles):
                if enabled is not None and not enabled[i] and i != current_index:
                    continue
                self.song_combo.addItem(title or f"Song {i + 1}", i)
                if i == current_index:
                    self.song_combo.setCurrentIndex(self.song_combo.count() - 1)
        finally:
            self._populating_setlist = False

    def set_export_midi_enabled(self, enabled: bool) -> None:
        """Enable or disable the export-MIDI menu action (on the Export split
        button and on its ☰ twin, which stands in while the bar is folded)."""
        if self.export_midi_action:
            self.export_midi_action.setEnabled(enabled)
            self.logger.debug(f"Export MIDI button {'enabled' if enabled else 'disabled'}")
        if self.export_midi_menu_action:
            self.export_midi_menu_action.setEnabled(enabled)

    def set_dock_stage_enabled(self, enabled: bool) -> None:
        """Enable the ☰ "Dock stage" action (only while the stage is floating)."""
        if self.dock_stage_action:
            self.dock_stage_action.setEnabled(enabled)

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