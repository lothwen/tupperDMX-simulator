# ABOUTME: MainWindow — the desktop view over app.core.AppCore: builds the
# ABOUTME: widget tree and wires the widget halves of the core's signals. The
# ABOUTME: thin entry point lives in main.py.
import os
import time
from dataclasses import asdict
from functools import partial

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QPushButton, QFileDialog, QSplitter, QMessageBox,
                             QStyle, QTabWidget, QDialog, QInputDialog,
                             QAbstractItemView, QAbstractSlider,
                             QAbstractSpinBox, QComboBox, QLineEdit,
                             QPlainTextEdit, QTabBar, QTextEdit)
from PyQt6.QtGui import QGuiApplication, QShortcut, QKeySequence
from PyQt6.QtCore import (Qt, QSize, QTimer, QEvent, QVariantAnimation,
                          QAbstractAnimation, QEasingCurve)

from app.core import AppCore
from app.media_controller import MONITOR_GUIDE
from audio.mixer import BUS_VOICE
from core.config import Config, shipped_fixtures
from core.io_routing import midi_routing_from_io
from core.paths import default_shows_dir
from core.show import store as show_store
from core.show.model import ShowError
from timeline.manager import Timeline
from tupperlogger import get_logger
from ui.widgets import icons
from ui.about_dialog import AboutDialog
from ui.clip_library import ClipLibraryWidget
from ui.dmx_display import DMXDisplayWidget
from ui.error_dialogs import ErrorDialogManager
from ui.fixture.browser import AddFixtureDialog
from ui.fixture.designer import StageEditToolbar, FixtureInspector
from ui.playlist_controller import PlaylistController
from ui.settings_dialog import SettingsDialog
from ui.stage_window import StageWindow
from stage.controller import StageController
from ui import theme
from ui.timeline_toolbar import TimelineToolbar
from ui.toolbar_manager import ToolbarManager
from ui.markers_dialog import MarkersDialog
from ui.offset_dialog import (CHOICE_AUDIO_ONLY, CHOICE_CANCEL,
                              CHOICE_MOVE_SHOW, OffsetRippleDialog)
from core import markers as core_markers
from core import note_layout
from webremote.state import probe_duration, song_duration

# ←/→ scrub the playhead. A tap nudges by ARROW_SEEK_STEP; holding ramps the
# step up to ARROW_SEEK_MAX_STEP over ARROW_SEEK_RAMP seconds of hold. The
# OS key-repeat rate is not ours to pick, so ARROW_SEEK_INTERVAL sets the pace
# instead: every step is a real seek, repositioning MIDI, audio and the
# metronome, and none of that wants to run at a 30 Hz repeat rate.
ARROW_SEEK_STEP = 0.5
ARROW_SEEK_MAX_STEP = 5.0
ARROW_SEEK_RAMP = 3.0
ARROW_SEEK_INTERVAL = 0.08

# Widgets that type text, so a Space or an arrow is a keystroke for them and
# never a transport command.
TEXT_ENTRY_WIDGETS = (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)

# The audio formats the app loads. One list behind the load-time check and the
# drop target's accept test, so the window never accepts a drag it will then
# refuse to load.
AUDIO_SUFFIXES = ('.mp3', '.wav')


def is_audio_path(path):
    """True for a path this app can load as a backing or guide track."""
    return path.lower().endswith(AUDIO_SUFFIXES)


def audio_paths_from_mime(mime):
    """Local audio files carried by a drag, in the order they were dropped."""
    if not mime.hasUrls():
        return []
    return [url.toLocalFile() for url in mime.urls()
            if url.isLocalFile() and is_audio_path(url.toLocalFile())]


class MainWindow(QMainWindow):
    def __init__(self, config: Config, show=None):
        super().__init__()
        self.config = config
        self.logger = get_logger("ui.main")

        # The shared application core owns and wires every widget-free
        # collaborator (show, audio engine, playback, MIDI, DMX, playlist,
        # web remote) — headless mode runs the same object without a window.
        # The aliases below keep the existing widget-wiring call sites
        # unchanged: the window is a view over the core.
        core = self.app_core = AppCore(config, show)
        self.show_manager = core.show_manager
        self.bpm_state = core.bpm_state
        self.audio_engine = core.audio_engine
        self.dmx_manager = core.dmx_manager
        self.midi_manager = core.midi_manager
        self.media_controller = core.media_controller
        self.audio_controller = core.audio_controller
        self.playlist_core = core.playlist_controller
        self.webremote_manager = core.webremote_manager

        # Widget halves of the core's signals (the core already handled the
        # logic half before these fire).
        core.software_controller_changed.connect(self._on_software_controller_ui)
        core.patch_reloaded.connect(self._on_patch_reloaded)
        core.show_reloaded.connect(self._on_show_reloaded)
        core.config_save_failed.connect(self._on_config_save_failed)
        core.prefs_reloaded.connect(self._on_prefs_reloaded)
        self.dmx_manager.dmx_data_received.connect(self.update_dmx)
        self.dmx_manager.error_occurred.connect(self.on_dmx_error)
        self.midi_manager.error_occurred.connect(self.on_midi_error)
        self.midi_manager.data_changed.connect(self.on_midi_data_changed_from_manager)

        # Playlist UI layer: widget refresh + relink dialogs over the core's
        # PlaylistCore (aliased as playlist_core above).
        self.playlist_controller = PlaylistController(self)

        # Initialize UI components
        self._setup_ui_components()

        # Window setup
        self.update_window_title()
        # A floor, not the real minimum: Qt takes the larger of this and what
        # the children need, and both toolbars now fold themselves to fit a
        # 1280-wide display. Declaring 1200x800 here used to add a height floor
        # taller than the 1280x800 screens this is meant to run on.
        self.setMinimumSize(1024, 600)
        # Audio files can be dropped anywhere on the window (see dropEvent).
        self.setAcceptDrops(True)
        self.setGeometry(100, 100, self.config.window.width, self.config.window.height)

        # Setup main layout
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QVBoxLayout(self.central_widget)
        self.main_layout.setContentsMargins(0, 0, 0, 0)  # Remove all margins
        self.main_layout.setSpacing(0)  # Remove spacing between widgets

        # The Edit-fixture dialog while it is up, so a second request queued
        # behind the first raises it instead of opening a rival.
        self._fixture_editor = None

        # Create UI
        self.setup_ui()

        # Live-patch baseline: the last-applied patch signature, so
        # _on_patch_changed only rebuilds the views on real changes (the
        # controller's half keeps its own, in AppCore.on_patch_edited).
        self._last_ui_sig = self._patch_ui_signature()

        # Widget fan-out of BPM changes (the metronome/count-in half already
        # rides bpm_state inside the core). Connected before the initial song
        # load, which is gated by begin/end_song_load like any other load.
        self.bpm_state.bpm_changed.connect(self._on_bpm_state_changed)
        # The ruler/grid re-grid off the MAP (base tempo + tempo markers), not
        # off the base BPM alone — a tempo marker changes one without the other.
        self.app_core.tempo_map_changed.connect(self.timeline.set_tempo_map)

        # Bring the core up: current song (or empty state), persisted I/O
        # routing, web remote.
        self.app_core.start()

        # Update song counter in toolbar
        self.playlist_controller.update_song_counter()

        # Install global event filter for spacebar / Esc / arrow handling
        QApplication.instance().installEventFilter(self)

        # Arrow-key scrub: when the current hold began and when its last step
        # landed (see _arrow_seek).
        self._arrow_held_since = 0.0
        self._arrow_last_step = 0.0

        # Start with the timeline holding focus. Left to itself the focus chain
        # opens on the setlist combo, which is an editable rename field — so
        # ←/→ (and Space) would go to its caret and the transport would look
        # dead until the operator clicked somewhere else.
        self.timeline.content_view.setFocus()

        # Undo / redo shortcuts (Ctrl+Z / Ctrl+Shift+Z)
        self.undo_shortcut = QShortcut(QKeySequence.StandardKey.Undo, self)
        self.undo_shortcut.activated.connect(self._on_undo)
        self.redo_shortcut = QShortcut(QKeySequence.StandardKey.Redo, self)
        self.redo_shortcut.activated.connect(self._on_redo)

    def _on_undo(self):
        """Undo the last timeline edit."""
        if not self.midi_manager.undo():
            self.logger.debug("Nothing to undo")

    def _on_redo(self):
        """Redo the last undone timeline edit."""
        if not self.midi_manager.redo():
            self.logger.debug("Nothing to redo")

    @property
    def software_controller(self):
        """The running software DMX controller (owned by the core)."""
        return self.app_core.software_controller

    def _setup_ui_components(self):
        """Initialize all UI component managers and connect the widget-facing
        halves of the core controllers' signals."""
        # Get icons
        play_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        pause_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPause)
        stop_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop)

        self.media_controller.playback_state_changed.connect(self._on_playback_state_changed)
        self.media_controller.position_changed.connect(self._on_position_changed)
        self.media_controller.error_occurred.connect(self._on_media_error)

        self.dmx_display = DMXDisplayWidget(self.config)
        self.dmx_display.channel_value_edited.connect(self._on_dmx_channel_edited)
        self.clip_library = ClipLibraryWidget(self.config, self.show_manager)
        self.clip_library.preview_requested.connect(self.app_core.preview_clip)
        # A card's menu or the "+ New" tile. Deferred inside the opener: the
        # dialog can rebuild the very card that asked.
        self.clip_library.edit_fixture_requested.connect(self.open_fixture_editor)

        # The core already logged it; the window only owes the dialog.
        self.audio_controller.audio_error_occurred.connect(
            partial(ErrorDialogManager.show_audio_hardware_error, self))

        self.stage_controller = StageController(self.config)

        self.toolbar_manager = ToolbarManager(self.config)
        # Transport lives in a toolbar above the timeline (built in setup_ui).
        self.timeline_toolbar = TimelineToolbar(play_icon, pause_icon, stop_icon)
        self._connect_toolbar_signals()

    def _connect_toolbar_signals(self):
        """Connect toolbar manager signals to appropriate handlers."""
        self.toolbar_manager.load_file_requested.connect(self.open_file_dialog)
        self.toolbar_manager.audio_clear_requested.connect(self.clear_backing_track)
        self.toolbar_manager.guide_clear_requested.connect(self.clear_guide_track)
        self.toolbar_manager.new_midi_track_requested.connect(self.create_new_midi_track)
        self.toolbar_manager.export_midi_requested.connect(self.export_midi_dialog)
        # Show project signals
        self.toolbar_manager.new_show_requested.connect(self.new_show_dialog)
        self.toolbar_manager.open_show_requested.connect(self.open_show_dialog)
        # Manual Save: a forced flush (autosave already covers the rest).
        self.toolbar_manager.save_show_requested.connect(
            partial(self.show_manager.flush, "manual"))
        self.toolbar_manager.save_show_as_requested.connect(self.save_show_as_dialog)
        self.toolbar_manager.snapshot_requested.connect(self.take_snapshot_dialog)
        self.toolbar_manager.restore_version_requested.connect(self.restore_version_from_toolbar)
        self.toolbar_manager.export_bundle_requested.connect(self.export_bundle_dialog)
        self.toolbar_manager.import_bundle_requested.connect(self.import_bundle_dialog)
        self.toolbar_manager.song_title_edited.connect(self.on_song_title_edited)
        self.toolbar_manager.song_key_edited.connect(self.app_core.set_song_key)
        self.show_manager.autosave_state_changed.connect(
            self.toolbar_manager.set_autosave_state)
        self.show_manager.versions_changed.connect(self._refresh_versions_ui)
        self.show_manager.midi_path_allocated.connect(
            self.toolbar_manager.update_midi_file_display)
        self.show_manager.setlist_changed.connect(
            self.playlist_controller.update_song_counter)
        # Transport signals now originate from the timeline toolbar.
        self.timeline_toolbar.play_pause_requested.connect(self.play_pause_playback)
        self.timeline_toolbar.stop_requested.connect(self.stop_playback)
        self.timeline_toolbar.bpm_changed.connect(self.bpm_changed)
        self.timeline_toolbar.song_precount_toggled.connect(
            self.app_core.on_song_precount_toggled)
        self.app_core.precount_changed.connect(
            self.timeline_toolbar.set_song_precount)
        # Editor monitor (backing | guide): toggle → controller → waveform.
        # The editor re-picks it per song (the guide when the song has one);
        # only the desktop does — headless always plays the backing.
        self.media_controller.set_auto_monitor(True)
        self.timeline_toolbar.monitor_changed.connect(
            self.media_controller.set_monitor)
        self.media_controller.monitor_changed.connect(self._on_monitor_changed)
        # Playlist navigation signals
        self.toolbar_manager.prev_song_requested.connect(self.playlist_controller.prev_song)
        self.toolbar_manager.next_song_requested.connect(self.playlist_controller.next_song)
        self.toolbar_manager.new_song_requested.connect(self.playlist_controller.add_empty_song)
        self.toolbar_manager.remove_song_requested.connect(self.playlist_controller.remove_current_song)
        self.toolbar_manager.song_selected.connect(self.playlist_controller.go_to_song)
        # Application menu signals
        self.toolbar_manager.settings_requested.connect(self.show_settings_dialog)
        self.toolbar_manager.about_requested.connect(self.show_about_dialog)
        self.toolbar_manager.dock_stage_requested.connect(self.dock_stage)
    
    def setup_ui(self):
        """Setup the main UI layout."""
        # Create and add toolbar
        toolbar = self.toolbar_manager.create_toolbar(self)
        self.addToolBar(toolbar)
        self._refresh_show_status()
        self._refresh_versions_ui()

        # Create main content splitter (stage | sidebar); added to the vertical
        # splitter below so the timeline section can be resized against it.
        self.main_content = QSplitter(Qt.Orientation.Horizontal)

        # Add stage view
        stage_view = self.stage_controller.get_stage_view()
        self.main_content.addWidget(stage_view)

        # Right sidebar: tabbed container hosting the clip library and the
        # DMX channel monitor. Library is the default tab — most users open
        # the app to edit clips, not to verify channel output.
        self.right_sidebar = QTabWidget()
        self.right_sidebar.setObjectName("RightSidebar")
        self.right_sidebar.addTab(self.clip_library, "Library")
        self.right_sidebar.addTab(self.dmx_display, "DMX Channels")
        self.main_content.addWidget(self.right_sidebar)

        # Create timeline with MidiManager reference
        self.timeline = Timeline(
            midi_manager=self.midi_manager,
            initial_metronome_volume=self.config.volume.metronome,
            initial_audio_volume=self.config.volume.audio,
            initial_voice_volume=self.config.volume.voice,
            initial_waveform_track_height=self.config.window.waveform_track_height,
            initial_midi_events_track_height=self.config.window.midi_events_track_height,
            initial_header_width=self.config.window.timeline_header_width,
            fixtures=self.config.fixtures,
            ui_scale=self.config.window.ui_scale,
        )
        self.timeline.set_audio_source(self._waveform_audio_info)
        # The decode the song change kicked off lands after the band is drawn.
        self.audio_engine.backing_loaded.connect(self._on_backing_decoded)
        self.timeline.seek_requested.connect(self.seek_playback)
        self.timeline.bpm_detected.connect(self.bpm_state.set_bpm)
        self.timeline.waveform_offset_changed.connect(self.on_waveform_offset_changed)
        self.timeline.waveform_offset_committed.connect(
            self.on_waveform_offset_committed)
        # The header's sliders run 0–100; the core takes 0.0–1.0, applies it
        # to the bus and persists it.
        self.timeline.metronome_volume_changed.connect(
            lambda value: self.app_core.set_volume(metronome=value / 100.0))
        self.timeline.metronome_mute_toggled.connect(
            self.audio_controller.set_metronome_muted)
        self.timeline.audio_volume_changed.connect(
            lambda value: self.app_core.set_volume(audio=value / 100.0))
        self.timeline.audio_mute_toggled.connect(self.media_controller.set_audio_muted)
        self.timeline.voice_volume_changed.connect(
            lambda value: self.app_core.set_volume(voice=value / 100.0))
        self.timeline.voice_mute_toggled.connect(
            partial(self.audio_engine.set_bus_muted, BUS_VOICE))
        self.timeline.track_heights_changed.connect(self.on_track_heights_changed)
        self.timeline.header_width_changed.connect(self.on_header_width_changed)
        self.timeline.track_order_changed.connect(self.on_track_order_changed)
        # A lane header wants its fixture edited. Deferred inside the opener:
        # the dialog can rebuild the very header that asked.
        self.timeline.fixture_edit_requested.connect(self.open_fixture_editor)
        # The clip editor can write a fixture's scene banks, which the library
        # panel has already read — rebuild it rather than wait for a restart.
        self.timeline.library_changed.connect(self._refresh_clip_library)
        # Auditioning a clip from the editor takes the same route as the clip
        # library's click-to-preview, so config.io routing applies.
        self.timeline.preview_requested.connect(self.app_core.preview_clip)
        self.timeline.marker_move_committed.connect(self.on_marker_moved)
        self.timeline.marker_edit_requested.connect(self.on_marker_edit_requested)
        # Timeline no longer emits midi_data_changed - handled by MidiManager
        # Wire the timeline toolbar's view controls to the timeline view, and
        # mirror live zoom (Fit / Ctrl+wheel) back onto the toolbar slider.
        self.timeline_toolbar.fit_requested.connect(self.timeline.fit_to_width)
        self.timeline_toolbar.zoom_changed.connect(self.timeline.set_zoom)
        self.timeline.zoom_changed.connect(self.timeline_toolbar.set_zoom_value)
        self.timeline_toolbar.row_height_changed.connect(self._on_row_height_preset)
        self.timeline_toolbar.collapse_all_requested.connect(self._on_collapse_all_tracks)
        self.timeline_toolbar.ruler_mode_changed.connect(self.timeline.set_ruler_mode)
        self.timeline_toolbar.beat_division_changed.connect(self.timeline.set_beat_division)
        self.timeline_toolbar.snap_toggled.connect(self.timeline.set_snap_enabled)
        self.timeline_toolbar.markers_requested.connect(self.show_markers_dialog)
        self.timeline_toolbar.add_track_requested.connect(self.add_fixture_from_timeline)

        # Bundle the timeline toolbar + tracks into one resizable section. The
        # layout is kept because undocking flips the two (see _set_toolbar_at_bottom).
        timeline_section = QWidget()
        self._timeline_section_layout = QVBoxLayout(timeline_section)
        self._timeline_section_layout.setContentsMargins(0, 0, 0, 0)
        self._timeline_section_layout.setSpacing(0)
        self._timeline_section_layout.addWidget(self.timeline_toolbar)
        self._timeline_section_layout.addWidget(self.timeline)
        timeline_section.setMinimumHeight(160)

        # The timeline shares a row with nothing while the stage is docked. Once
        # it is undocked the right sidebar moves in beside it, so the timeline
        # gets the whole window height and the library a full-height column.
        self.timeline_row = QSplitter(Qt.Orientation.Horizontal)
        self.timeline_row.addWidget(timeline_section)

        # Vertical splitter: drag the divider to resize the timeline section.
        self.vertical_splitter = QSplitter(Qt.Orientation.Vertical)
        self.vertical_splitter.addWidget(self.main_content)
        self.vertical_splitter.addWidget(self.timeline_row)
        self.vertical_splitter.setCollapsible(0, False)
        self.vertical_splitter.setCollapsible(1, False)
        self.main_layout.addWidget(self.vertical_splitter, 1)

        section_height = self.config.window.timeline_section_height
        self.vertical_splitter.setSizes(
            [max(160, self.height() - section_height), section_height])
        # self.height() is the whole window, but the splitter only fills the
        # central widget (the top toolbar sits above it). Sizing from the window
        # height over-counts by the toolbar, so Qt scales the request down and
        # the section loses ~the toolbar's height on load. Re-apply once the
        # splitter has real geometry, using its own available height.
        QTimer.singleShot(0, self._apply_persisted_section_height)
        self.vertical_splitter.splitterMoved.connect(self._on_vertical_splitter_moved)
        # Reflect the persisted row state. In collapsed ("S") mode the tracks
        # must be re-collapsed so their 30px lanes match the section height that
        # was fitted to them; otherwise 50px lanes would overflow the small
        # section. Otherwise reflect the persisted track height on M/L.
        if self.config.window.tracks_collapsed:
            self.timeline.set_all_tracks_collapsed(True)
            self.timeline_toolbar.set_rows_collapsed()
        else:
            self.timeline_toolbar.set_row_height(self.config.window.midi_events_track_height)

        # The width the user left the sidebar at. Never dragged, and its
        # opening width follows the UI scale instead — its tab labels are
        # styled, so they grow with it and 250px stops holding them.
        self._sidebar_width = (self.config.window.sidebar_width
                               or round(250 * self.config.window.ui_scale))
        self.main_content.setSizes(
            [self.width() - self._sidebar_width, self._sidebar_width])
        # Either splitter can host the sidebar (see _sidebar_splitter below).
        self.main_content.splitterMoved.connect(self._on_sidebar_splitter_moved)
        self.timeline_row.splitterMoved.connect(self._on_sidebar_splitter_moved)

        # Floating toggle button for the DMX side panel
        self._dmx_panel_collapsed = False
        self._last_dmx_width = self._sidebar_width
        self._panel_animation = None
        # Which splitter currently hosts the sidebar — main_content while the
        # stage is docked, timeline_row once it is undocked.
        self._sidebar_splitter = self.main_content
        self.dmx_toggle_button = QPushButton(stage_view)
        self.dmx_toggle_button.setObjectName("DmxPanelToggle")
        self.dmx_toggle_button.setFixedSize(28, 28)
        self.dmx_toggle_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dmx_toggle_button.clicked.connect(self._toggle_dmx_panel)
        stage_view.add_overlay(self.dmx_toggle_button, anchor="top-right", reference="viewport")
        self.dmx_toggle_button.show()
        self._update_dmx_toggle_button()

        # Stage scenario designer: an "Edit stage" button pinned to the stage
        # image's top-right corner, swapped for a top-left toolbar while editing.
        self.edit_stage_button = QPushButton("Edit stage")
        self.edit_stage_button.setIcon(icons.icon("pencil", "#ffffff"))
        self.edit_stage_button.setObjectName("EditStageButton")
        self.edit_stage_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edit_stage_button.clicked.connect(self.enter_edit_mode)
        self.edit_stage_button.adjustSize()
        stage_view.add_overlay(self.edit_stage_button, anchor="top-right", reference="background")
        self.edit_stage_button.show()

        # Detach the stage into its own window (a second display, typically),
        # which hands the whole main window to the timeline. Registered after
        # "Edit stage" so the overlay row puts it to that button's left, and
        # kept visible in edit mode — the stage is designable either way.
        self._stage_window = None
        self._docked_content_sizes = None
        self._collapsed_before_undock = False
        self.dock_stage_button = QPushButton()
        self.dock_stage_button.setObjectName("StageDockButton")
        self.dock_stage_button.setIconSize(QSize(18, 18))
        self.dock_stage_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dock_stage_button.clicked.connect(self._toggle_stage_dock)
        self._update_dock_stage_button()
        self.dock_stage_button.adjustSize()
        stage_view.add_overlay(self.dock_stage_button, anchor="top-right", reference="background")
        self.dock_stage_button.show()

        self.edit_toolbar = StageEditToolbar(
            self.stage_controller, self._on_patch_changed, self.exit_edit_mode,
            midi_manager=self.midi_manager)
        # The fixture browser's Scenes tab writes a type's scene banks whichever
        # button closes it, so the library panel is rebuilt on the write, not on
        # OK — the same route the timeline's clip editor takes.
        self.edit_toolbar.library_changed.connect(self._refresh_clip_library)
        stage_view.add_overlay(self.edit_toolbar, anchor="top-left", reference="background")
        self.edit_toolbar.hide()

        # Per-selection inspector, pinned to the stage image's top-right; shown
        # only while a fixture is selected (which only happens in edit mode).
        self.fixture_inspector = FixtureInspector(
            self.stage_controller, self._on_patch_changed)
        stage_view.add_overlay(self.fixture_inspector, anchor="top-right", reference="background")
        self.fixture_inspector.hide()
        self.stage_controller.fixture_selected.connect(self._on_fixture_selected)

        # Apply persisted collapsed state once the splitter has a real geometry
        if self.config.window.dmx_panel_collapsed:
            QTimer.singleShot(0, self._apply_initial_collapsed_state)

    def _apply_initial_collapsed_state(self) -> None:
        """Collapse the DMX panel without animation at startup."""
        self._set_sidebar_collapsed(True)

    def _set_sidebar_collapsed(self, collapsed: bool) -> None:
        """Collapse/expand the sidebar in whichever splitter now hosts it.

        The animation-free half of `_toggle_dmx_panel`, so startup and the
        undock re-flow can reach the same state without a 200ms slide.
        """
        splitter = self._sidebar_splitter
        sizes = splitter.sizes()
        if len(sizes) < 2:
            return
        total = sizes[0] + sizes[1]
        if collapsed:
            self._last_dmx_width = sizes[1] if sizes[1] > 0 else self._last_dmx_width
        # Visible first: a hidden child gets no width from the splitter.
        self.right_sidebar.setVisible(not collapsed)
        if collapsed:
            splitter.setSizes([total, 0])
        else:
            width = max(0, min(self._last_dmx_width or 250, total - 200))
            splitter.setSizes([total - width, width])
        self._dmx_panel_collapsed = collapsed
        self._update_dmx_toggle_button()

    def _toggle_dmx_panel(self) -> None:
        """Slide the DMX side panel in or out with a short animation."""
        if (self._panel_animation is not None
                and self._panel_animation.state() == QAbstractAnimation.State.Running):
            return

        splitter = self._sidebar_splitter
        sizes = splitter.sizes()
        if len(sizes) < 2:
            return
        total = sizes[0] + sizes[1]

        will_be_collapsed = not self._dmx_panel_collapsed

        if will_be_collapsed:
            if sizes[1] > 0:
                self._last_dmx_width = sizes[1]
            start_value = sizes[1]
            end_value = 0
        else:
            self.right_sidebar.setVisible(True)
            target = self._last_dmx_width if self._last_dmx_width > 0 else 250
            # Always leave at least 200px for the stage area
            target = max(0, min(target, total - 200))
            start_value = sizes[1]
            end_value = target

        anim = QVariantAnimation(self)
        anim.setDuration(200)
        anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        anim.setStartValue(start_value)
        anim.setEndValue(end_value)
        anim.valueChanged.connect(
            lambda val: splitter.setSizes([total - int(val), int(val)])
        )

        def on_finished():
            self._dmx_panel_collapsed = will_be_collapsed
            if will_be_collapsed:
                self.right_sidebar.setVisible(False)
            self._update_dmx_toggle_button()
            self.config.window.dmx_panel_collapsed = will_be_collapsed
            self.save_config()

        anim.finished.connect(on_finished)
        self._panel_animation = anim
        anim.start()

    def _update_dmx_toggle_button(self) -> None:
        """Refresh the toggle button glyph and tooltip based on current state."""
        if self._dmx_panel_collapsed:
            self.dmx_toggle_button.setIcon(icons.icon("chevron-left", "#ffffff"))
            self.dmx_toggle_button.setToolTip("Show DMX panel")
        else:
            self.dmx_toggle_button.setIcon(icons.icon("chevron-right", "#ffffff"))
            self.dmx_toggle_button.setToolTip("Hide DMX panel")

    def on_waveform_offset_changed(self, offset_sec):
        """Live preview of a waveform drag — a LABEL and nothing else.

        This fires on every mouse-move frame, so it must stay free of anything
        with a side effect: the drag is a proposal until the release, where the
        dialog decides what it meant. In particular it does NOT move the
        controller's offsets, which is what keeps a drag out of the audio path
        (and out of the log) and what lets Cancel put everything back by simply
        repositioning the item.

        The SETLIST chip shows the BACKING offset, so a guide drag leaves it
        alone; the chip is also why this runs at all on a programmatic
        set_waveform_offset (song load, monitor switch, Cancel's revert).
        """
        if not self._monitoring_guide():
            self.toolbar_manager.update_offset_display(offset_sec)

    def on_waveform_offset_committed(self, old_sec, new_sec):
        """A waveform drag finished: ask what should follow the audio, then
        persist that decision.

        The offset moves ONE audio track (`audio_time = timeline_time -
        active_offset()`); every clip, song part and marker is anchored in
        timeline seconds, and the other track has its own offset — so nothing
        else moves on its own. That is what you want the first time a track is
        aligned, and wrong every time after, which is why the drag asks.

        Nothing is written before the answer: Cancel has to be able to put the
        offset back, and the drag itself only ever changed it in memory.
        """
        mc = self.media_controller
        moved_guide = self._monitoring_guide()
        choice, move_other, offset = self._ask_offset_ripple(
            old_sec, new_sec, moved_guide)

        if choice == CHOICE_CANCEL:
            # The controller never saw the drag, so putting the item back (which
            # re-emits the preview, restoring the chip) is the whole revert.
            self.timeline.set_waveform_offset(old_sec)
            return

        if offset != new_sec:
            # Advanced overrode where the drag landed; the item has to agree.
            self.timeline.set_waveform_offset(offset)

        if moved_guide:
            mc.set_guide_offset(offset)
            self.config.update_current_song(guide_offset=offset)
        else:
            mc.set_audio_offset(offset)
            self.config.update_current_song(offset=offset)
            self.toolbar_manager.update_offset_display(offset)

        delta = offset - old_sec
        if move_other:
            self._move_other_track(moved_guide, delta)
        if choice == CHOICE_MOVE_SHOW:
            self.move_show_with_audio(delta)
        self.save_config()

    def _monitoring_guide(self) -> bool:
        """Whether the waveform band is showing (and so editing) the guide."""
        mc = self.media_controller
        return mc.monitor == MONITOR_GUIDE and bool(mc.guide_file)

    def _other_track(self, moved_guide: bool):
        """`(label, current offset)` of the track the drag did NOT move, or
        `(None, None)` when there is none — the checkbox has nothing to offer
        for a song with one track. The offset comes along because the dialog
        quotes absolutes: it shows where that track would LAND, not by how far
        it would shift."""
        mc = self.media_controller
        if moved_guide:
            return (("backing track", mc.audio_offset_sec) if mc.audio_file
                    else (None, None))
        return (("guide track", mc.guide_offset_sec) if mc.guide_file
                else (None, None))

    def _move_other_track(self, moved_guide: bool, delta: float) -> None:
        """Slide the track the drag did not touch by the same delta.

        Relative, not absolute: the two files have their own lead-ins, so what
        transfers between them is the CORRECTION, never the offset itself."""
        mc = self.media_controller
        if moved_guide:
            offset = mc.audio_offset_sec + delta
            mc.set_audio_offset(offset)
            self.config.update_current_song(offset=offset)
            self.toolbar_manager.update_offset_display(offset)
        else:
            offset = mc.guide_offset_sec + delta
            mc.set_guide_offset(offset)
            self.config.update_current_song(guide_offset=offset)
        self.logger.info(
            f"Moved the {'backing' if moved_guide else 'guide'} track with it "
            f"({delta:+.3f}s -> {offset:+.3f}s)")

    def _show_start(self):
        """`(time, label)` of the earliest thing the show has anywhere on this
        song's timeline — a clip, a song part or a marker — or None when the
        song is still empty. The label is what the offset dialog quotes when
        that item is the one keeping the show from following a drag left.
        BLACKOUTs never count: a shift drops the ones it pushes before t=0."""
        song = self.config.get_current_song()
        firsts = []
        events = [e for e in self.midi_manager.midi_events
                  if not self.midi_manager.is_blackout(e)]
        if events:
            event = min(events, key=lambda e: e["time"])
            firsts.append((event["time"], self._describe_event(event)))
        marker = core_markers.earliest_marker(song)
        if marker is not None:
            firsts.append(marker)
        return min(firsts, key=lambda first: first[0]) if firsts else None

    def _describe_event(self, event) -> str:
        """Name one timeline event the way its lane shows it: the song part, or
        the scene/clip label plus the fixture whose lane it sits on and that
        lane's MIDI channel."""
        note = int(event["note"])
        label = getattr(event.get("info"), "part", None) or f"Note {note}"
        if note_layout.is_song_part(note):
            return f"song part “{label}”"
        if event.get("group_id"):
            what = f"scene “{event.get('scene_name') or 'Scene'}”"
        else:
            what = f"clip “{label}”"
        channel = event.get("channel")
        if channel is None:
            return what
        fixture = next((f for f in self.config.fixtures
                        if f.channel == channel), None)
        where = f" on {fixture.name or fixture.type}" if fixture else ""
        return f"{what}{where} (MIDI channel {channel})"

    def _ask_offset_ripple(self, old_sec: float, new_sec: float,
                           moved_guide: bool):
        """Ask what follows the track a drag moved. Returns
        `(choice, move_other_track, offset)`.

        Silent — and AUDIO_ONLY at the dragged offset — when there is nothing
        to offer: aligning a lone track on a song with no clips or markers is
        the normal first step and must not nag.
        """
        start = self._show_start()
        other_label, other_offset = self._other_track(moved_guide)
        if start is None and other_label is None:
            return CHOICE_AUDIO_ONLY, False, new_sec

        start_time, start_item = start if start is not None else (None, None)
        dialog = OffsetRippleDialog(
            self,
            moved_label="guide track" if moved_guide else "backing track",
            other_label=other_label, other_offset=other_offset or 0.0,
            old_offset=old_sec, new_offset=new_sec,
            show_start=start_time, show_start_item=start_item,
            # Beats are counted at the tempo sounding where the track started.
            bpm=self.bpm_state.tempo_map().bpm_at(old_sec))
        dialog.exec()
        return dialog.choice(), dialog.move_other(), dialog.offset()

    def move_show_with_audio(self, delta: float) -> None:
        """Slide everything authored against the music by `delta`: the whole
        sequence and the whole marker track, in that order.

        Both halves are checked to be movable before either one moves (see
        OffsetRippleDialog) — a shift that applied to only one of them
        would put the clips and the markers out of sync with each other, which
        is worse than the staleness this fixes."""
        song = self.config.get_current_song()
        moved_events = self.midi_manager.shift_all(delta)
        moved_markers = core_markers.shift_markers(song, delta) if song else False
        if moved_markers:
            # Persists, redraws the ruler and re-applies the tempo map / cues.
            self.apply_marker_edits(song)
        elif moved_events:
            self.save_config()
        if moved_events or moved_markers:
            self.logger.info(
                f"Moved the show with the audio by {delta:+.3f}s "
                f"(events={moved_events}, markers={moved_markers})")

    def _waveform_audio_info(self, audio_file):
        """(duration, PCM) for the timeline's waveform band.

        The duration is a mutagen header read, so the band is laid out at the
        right width immediately. The samples are only ever the ones the engine
        already decoded to PLAY this file — the timeline draws
        media_controller.active_file(), which is exactly what the engine
        loads, so the path check is enough to keep guide and backing apart.
        None while that decode is still running; _on_backing_decoded redraws."""
        if not audio_file:
            return None, None
        engine = self.audio_engine
        pcm = (engine.backing_samples()
               if engine.loaded_path() == audio_file else None)
        return probe_duration(audio_file), pcm

    def _on_backing_decoded(self, path):
        """Engine finished decoding: fill in the waveform it was drawn without."""
        if path == self.timeline.audio_file:
            self.timeline.redraw_waveform()

    def _on_monitor_changed(self, monitor):
        """Editor monitor switched: redraw the waveform for the monitored
        track and refresh the toggle state."""
        mc = self.media_controller
        active = mc.active_file()
        if active and os.path.exists(active):
            self.timeline.set_files(active)
            # Use QTimer to ensure waveform is drawn before setting offset
            QTimer.singleShot(
                100, lambda: self.timeline.set_waveform_offset(mc.active_offset()))
        else:
            self.timeline.set_files(None)
        song = self.config.get_current_song()
        has_guide = bool(song and song.guide and os.path.exists(song.guide))
        self.timeline_toolbar.set_monitor_state(monitor, has_guide)
    
    def bpm_changed(self, value):
        """Handle BPM changes from toolbar."""
        # Validate BPM range
        if not (40 <= value <= 300):
            ErrorDialogManager.show_validation_error(
                self, "BPM", str(value), "a value between 40 and 300"
            )
            # Reset to valid range
            value = max(40, min(300, value))
            self.timeline_toolbar.set_bpm(value)
            return
        self.bpm_state.set_bpm(value)

    def _on_bpm_state_changed(self, value):
        """Widget half of a BPM change (metronome/count-in ride bpm_state in
        the core): ruler, toolbar chip, spinbox view sync. BpmState dedupes,
        so the spinbox echo can't loop."""
        self.timeline.set_bpm(value)
        self.toolbar_manager.set_song_bpm(value)
        self.timeline_toolbar.set_bpm(value)

    def _on_playback_state_changed(self, is_playing):
        """Handle playback state changes from media controller."""
        self.timeline_toolbar.update_play_pause_icon(is_playing)
        # A clip editor's live output must not fight the sequencer for the
        # same DMX slots, so it is disarmed for the duration.
        self.timeline.set_transport_playing(is_playing)
        
        if is_playing:
            # Start metronome synchronized to current timeline position
            current_position = self.media_controller.get_current_position()
            self.audio_controller.start_metronome(current_position)
        else:
            self.audio_controller.stop_metronome()
    
    def _on_position_changed(self, position_seconds):
        """Handle position changes from media controller. Negative time is the
        count-in: the playhead waits at 0 and the display counts down."""
        self.timeline.update_playhead(max(0.0, position_seconds))
        if position_seconds < 0:
            self.timeline_toolbar.update_time_display(
                f"COUNT-IN −{-position_seconds:04.1f}")
            return
        # Update toolbar time display
        minutes = int(position_seconds // 60)
        seconds = position_seconds % 60
        time_str = f"{minutes:02d}:{seconds:06.3f}"
        self.timeline_toolbar.update_time_display(time_str)
    
    def _on_media_error(self, error_message, file_path=None):
        """Handle media errors from media controller (the core logged them)."""
        ErrorDialogManager.show_media_file_error(
            self, file_path or "Unknown file", error_message)

    def on_dmx_error(self, source, error):
        """Handle DMX errors (the core logged them) with a retry dialog."""
        if ErrorDialogManager.show_dmx_connection_error(self, source, error):
            # User wants to retry: re-apply the stage-input leg from config.io
            self.dmx_manager.restart_stage_input()

    def on_midi_error(self, port_name, error):
        """Handle MIDI errors (the core logged them) with a retry dialog."""
        if ErrorDialogManager.show_midi_connection_error(self, port_name, error):
            # User wants to retry: re-apply the routing (restarts a live worker)
            self.media_controller.set_midi_routing(midi_routing_from_io(self.config.io))

    def _on_software_controller_ui(self, controller):
        """Widget half of a software-controller change (the core already
        pointed the MIDI stream at it and seeded its auto state)."""
        # Live channel editing in the DMX monitor only works when a software
        # controller is present to receive the overrides (its universe size caps
        # which channels are editable).
        self.dmx_display.set_editing_enabled(
            controller is not None,
            len(controller.present) if controller is not None else 0)
        # Re-sync the 'A' buttons to the seeded/restored state.
        self.timeline.refresh_auto_buttons()
        # Redraw so the auto-mode song-part visualization appears when a
        # software controller attaches and clears when it detaches.
        self.timeline._schedule_refresh()

    def _on_dmx_channel_edited(self, channel, value):
        """Apply a manual value dragged in the DMX monitor to the live software
        controller. Transient: the normal workflow (scenes, chases, MIDI)
        overwrites it on its next frame; a no-op if no controller is active."""
        controller = self.software_controller
        if controller is not None:
            controller.set_manual_channel(channel, value)

    def update_dmx(self, dmx_data):
        """Update DMX data across all components."""
        # Update stage fixtures
        self.stage_controller.update_fixtures_with_dmx(dmx_data)
        
        # Update DMX display
        self.dmx_display.update_dmx_data(dmx_data)

    def show_settings_dialog(self):
        """Open the preferences editor and apply any changes live."""
        cfg = self.config
        # What the dialog can change, read afresh each time (it hands back new
        # objects), and what re-applies it — run in THIS order when it did.
        watched = (
            # Build or drop the GL context first: the light layer that fog is
            # then handed to is the one this call decides the existence of.
            (lambda: cfg.window.stage_gpu, self._apply_stage_gpu),
            # The dialog hands back a NEW fog object, so the light layer is
            # still holding the old one until it is told otherwise.
            (lambda: asdict(cfg.window.fog), self.stage_controller.apply_stage_config),
            # The routing, or the Art-Net target the sinks and listener read.
            (lambda: (asdict(cfg.io), asdict(cfg.artnet)), self.app_core.apply_io_config),
            (lambda: asdict(cfg.webremote), self.webremote_manager.apply_config),
            (lambda: asdict(cfg.audio), self.app_core.apply_audio_config),
            # Level and folder both take effect now rather than at the next
            # launch: turning DEBUG on to chase something has to help with the
            # thing happening in front of you, and the page's own live pane
            # would otherwise sit tailing a file nothing writes to.
            (lambda: asdict(cfg.logging), self.app_core.apply_logging_config),
        )
        before = [read() for read, _ in watched]

        dialog = SettingsDialog(self.config, self, audio_engine=self.audio_engine)
        # The Interface page previews its slider live and puts the old scale
        # back if the dialog is cancelled, so this is connected either way.
        dialog.ui_scale_previewed.connect(self.apply_ui_scale)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        self.apply_ui_scale()
        for (read, apply), old in zip(watched, before):
            if read() != old:
                apply()

        self.save_config()
        self.logger.info("Settings updated")

    def _apply_stage_gpu(self):
        """Turn the stage's GPU renderer on or off, now rather than on the next
        launch. Says so when the box cannot honour it — the fallback is silent
        at startup, but here it is the answer to something just asked for."""
        wanted = self.config.window.stage_gpu
        if self.stage_controller.set_gpu_light(wanted) == wanted:
            return
        self.logger.warning("Could not turn the GPU renderer %s",
                            "on" if wanted else "off")
        if wanted:
            QMessageBox.warning(
                self, "GPU renderer unavailable",
                "The stage's light could not be moved onto the GPU on this "
                "machine.\n\nIt is still being drawn by the normal renderer. "
                "This usually means there is no usable OpenGL driver.")

    def apply_ui_scale(self, scale=None):
        """Show the interface at `scale`, or at whatever the config says.

        `theme.apply_scale` is the idempotent half — a live prefs reload calls
        this for any `window` edit, and re-applying the stylesheet re-polishes
        every widget in the app, which is not something nudging the haze slider
        should pay for.

        The toolbars cached what their expanded layouts cost at the old scale,
        so they have to measure themselves again — but only once Qt has
        delivered the style change, or they would measure the size everything
        still has. Hence the zero-delay timer rather than a direct call.
        """
        if scale is None:
            scale = self.config.window.ui_scale
        if theme.apply_scale(QApplication.instance(), scale):
            # Neither of these is styled, so neither follows a restyle: a
            # painted header strip, and a width the config only holds once the
            # divider has been dragged.
            self.timeline.set_ui_scale(scale)
            self._apply_sidebar_scale(scale)
            QTimer.singleShot(0, self._remeasure_toolbars)

    def _apply_sidebar_scale(self, scale):
        """Re-derive the sidebar's width from the UI scale.

        Only while it is still the derived width: once the divider has been
        dragged, `config.window.sidebar_width` holds a size the user chose and
        the scale has no business overruling it.
        """
        if self.config.window.sidebar_width:
            return
        self._sidebar_width = round(250 * scale)
        sizes = self._sidebar_splitter.sizes()
        if len(sizes) == 2 and sizes[1] > 0:
            self._sidebar_splitter.setSizes(
                [sum(sizes) - self._sidebar_width, self._sidebar_width])

    def _remeasure_toolbars(self):
        self.toolbar_manager.remeasure_breakpoint()
        self.timeline_toolbar.remeasure_breakpoint()

    def _on_prefs_reloaded(self, adopted):
        """View half of a live userprefs reload — the core has already applied
        its own. Mirrors what show_settings_dialog() re-applies, minus the
        window geometry, which belongs to the window rather than to the file."""
        if "window" in adopted:
            self.stage_controller.set_haze_baseline(self.config.window.stage_haze)
            if self.config.window.stage_gpu != (self.stage_controller.light_layer
                                                is not None):
                self.stage_controller.set_gpu_light(self.config.window.stage_gpu)
            self.stage_controller.apply_stage_config()
            self.apply_ui_scale()
        if "volume" in adopted:
            self.timeline.set_volumes(
                audio=self.config.volume.audio,
                metronome=self.config.volume.metronome,
                voice=self.config.volume.voice)

    def show_about_dialog(self):
        """Show the About dialog with build provenance."""
        dialog = AboutDialog(self)
        dialog.exec()

    def enter_edit_mode(self):
        """Enter the stage scenario designer: grid on, swap button for toolbar."""
        stage_view = self.stage_controller.get_stage_view()
        self.stage_controller.set_design_mode(True)
        stage_view.set_grid_visible(True)
        self.edit_stage_button.hide()
        self.edit_toolbar.show()
        self.edit_toolbar.adjustSize()
        stage_view.reposition_overlays()

    def exit_edit_mode(self):
        """Leave edit mode (the toolbar's Done button). Edits already persist
        immediately via _on_patch_changed, so this just restores playback view."""
        stage_view = self.stage_controller.get_stage_view()
        self.stage_controller.set_design_mode(False)
        stage_view.set_grid_visible(False)
        self.edit_toolbar.hide()
        self.edit_stage_button.show()
        stage_view.reposition_overlays()

    # ------------------------------------------------------------------
    # Stage docking
    # ------------------------------------------------------------------
    def _toggle_stage_dock(self):
        self.dock_stage() if self._stage_window is not None else self.undock_stage()

    def _update_dock_stage_button(self):
        floating = self._stage_window is not None
        self.dock_stage_button.setIcon(
            icons.icon("dock" if floating else "undock", "#ffffff"))
        self.dock_stage_button.setToolTip(
            "Dock the stage back into the main window" if floating
            else "Open the stage in its own window")

    def _stage_window_size(self) -> QSize:
        """The stage's own aspect ratio, as big as the screen comfortably takes.

        Sized from the SCENE, not from the docked view: the view is whatever
        shape the splitter left it and the stage letterboxes inside it, so
        carrying that size over opened the window with the black bars baked in.
        A window that matches the scene has no bars at all.
        """
        scene = self.stage_controller.scene.sceneRect()
        aspect = (scene.width() / scene.height()) if scene.height() else 1.5
        screen = self.screen() or QGuiApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None

        width = 1100
        if available is not None:
            width = min(width, int(available.width() * 0.6))
        height = int(width / aspect)
        if available is not None and height > available.height() * 0.8:
            height = int(available.height() * 0.8)
            width = int(height * aspect)
        return QSize(max(480, width), max(320, height))

    def _set_toolbar_at_bottom(self, at_bottom: bool) -> None:
        """Put the timeline toolbar under the tracks rather than over them.

        Undocked the timeline owns the whole window, and its controls sit
        better along the bottom edge — the tracks then start at the very top.
        `removeWidget` leaves the toolbar parented to the section, so this is a
        reorder rather than a rebuild.
        """
        layout = self._timeline_section_layout
        layout.removeWidget(self.timeline_toolbar)
        layout.insertWidget(1 if at_bottom else 0, self.timeline_toolbar)

    def undock_stage(self):
        """Move the stage into its own window, giving the timeline the height.

        The overlay buttons are children of the view's viewport, so they travel
        with it and need no handling here.
        """
        if self._stage_window is not None:
            return
        stage_view = self.stage_controller.get_stage_view()

        # A collapsed sidebar would be stranded: its toggle is a stage overlay,
        # which is about to leave with the stage. Remember the state rather than
        # persisting this expansion — docking restores what the user chose.
        self._collapsed_before_undock = self._dmx_panel_collapsed
        if self._dmx_panel_collapsed:
            self._set_sidebar_collapsed(False)

        self.stage_controller.suspend_gpu_light()
        self._docked_content_sizes = self.main_content.sizes()
        self._set_toolbar_at_bottom(True)

        # Sidebar first, while main_content still has its geometry to measure.
        self.timeline_row.addWidget(self.right_sidebar)
        self._sidebar_splitter = self.timeline_row
        # Keep whatever width it had rather than the default — the splitter
        # clamps it down anyway if the timeline toolbar's minimum needs the room.
        target = (self._docked_content_sizes[1] if len(self._docked_content_sizes) > 1
                  and self._docked_content_sizes[1] > 0 else self._last_dmx_width or 250)
        width = max(0, min(target, self.timeline_row.width() - 200))
        self.timeline_row.setSizes([self.timeline_row.width() - width, width])

        self._stage_window = StageWindow()
        self._stage_window.dock_requested.connect(self.dock_stage)
        # One statement: the view must never be left parentless.
        self._stage_window.layout().addWidget(stage_view)
        self._stage_window.resize(self._stage_window_size())
        self._stage_window.show()
        self._stage_window.raise_()

        # A hidden child takes no room in a splitter — this is what frees the
        # height for the timeline.
        self.main_content.setVisible(False)
        self.dmx_toggle_button.hide()   # it drives THIS window's sidebar

        self._update_dock_stage_button()
        self.toolbar_manager.set_dock_stage_enabled(True)
        # Deferred: the view needs a native window of its own before a GL
        # viewport can be built on it.
        QTimer.singleShot(0, self.stage_controller.resume_gpu_light)
        self.logger.info("Stage undocked into its own window")

    def dock_stage(self):
        """Put the stage back into the main window."""
        if self._stage_window is None:
            return
        stage_view = self.stage_controller.get_stage_view()
        self.stage_controller.suspend_gpu_light()
        self._set_toolbar_at_bottom(False)

        window, self._stage_window = self._stage_window, None
        window.dock_requested.disconnect()

        self.main_content.setVisible(True)
        self.main_content.insertWidget(0, stage_view)
        self.main_content.insertWidget(1, self.right_sidebar)
        self._sidebar_splitter = self.main_content
        self.main_content.setSizes(
            self._docked_content_sizes
            or [self.width() - self._sidebar_width, self._sidebar_width])

        self.dmx_toggle_button.show()
        if self._collapsed_before_undock:
            self._set_sidebar_collapsed(True)

        window.close_silently()
        window.deleteLater()

        self._update_dock_stage_button()
        self.toolbar_manager.set_dock_stage_enabled(False)
        QTimer.singleShot(0, self.stage_controller.resume_gpu_light)
        self.logger.info("Stage docked back into the main window")

    def add_fixture_from_timeline(self):
        """Timeline toolbar's "+ Add track": a timeline lane IS a patched
        fixture, so this enters the stage designer and opens the same fixture
        browser the edit toolbar's Add button uses. Edit mode stays on
        afterwards so the new fixture can be dragged into place."""
        self.enter_edit_mode()
        self.edit_toolbar.open_add_fixture()

    def open_fixture_editor(self, channel: int, tab: str = "scenes",
                            ref: dict = None) -> None:
        """The ONE entry point for the Edit-fixture dialog. `channel` is the
        fixture's stable MIDI channel (the binding key, not a display row).

        Deferred by a zero timer on purpose. Every caller emits from inside a
        widget this dialog can destroy: an edit in it runs _on_patch_changed,
        which rebuilds the timeline tracks — deleteLater()ing every
        TrackHeaderWidget — and refreshes the clip library, which
        deleteLater()s every pill and card. deleteLater posts to the event
        loop, and a modal exec() IS an event loop, so a dialog opened straight
        from a header's or a card's own handler returns into a freed C++
        object. Letting the emitting stack unwind first is the whole fix.
        """
        QTimer.singleShot(
            0, lambda: self._open_fixture_editor_now(channel, tab, ref))

    def _open_fixture_editor_now(self, channel: int, tab: str,
                                 ref: dict = None) -> None:
        if self._fixture_editor is not None:
            # A second request queued behind the first (an impatient
            # double-click): show the one that is already up.
            self._fixture_editor.raise_()
            self._fixture_editor.activateWindow()
            return
        instance = next((f for f in self.config.fixtures
                         if getattr(f, "channel", -1) == channel), None)
        if instance is None:
            # Repatched away between the request and this tick.
            self.logger.info(f"No fixture on channel {channel} to edit")
            return
        dialog = AddFixtureDialog(self.stage_controller, self, instance=instance,
                                  midi_manager=self.midi_manager)
        dialog.select_tab(tab)
        # A card names one scene or chase, not just a tab — open on it.
        dialog.reveal(ref)
        # The tabs write the library whichever button closes the dialog, so the
        # panel rebuilds on the write — the same route the edit toolbar takes.
        dialog.library_changed.connect(self._refresh_clip_library)
        self._fixture_editor = dialog
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            self._fixture_editor = None
        # A confirmed Delete already removed the fixture and accepted; anything
        # else the dialog holds is answered by OK and discarded by Cancel.
        if accepted and (dialog.was_deleted() or dialog.apply_edits()):
            self._on_patch_changed()
        dialog.deleteLater()

    def _on_fixture_selected(self, instance):
        """Show + populate the overlay inspector for the selected fixture.

        Gated on edit mode rather than assumed from the emission: a mode or
        rigging change also emits this, and the fixture dialog's edit mode
        drives both from the timeline and the clip library — with the stage not
        in the designer at all, where an inspector card popping up over it
        would be nonsense.
        `isHidden` rather than `isVisible`, for the same reason
        `_escape_leaves_edit_mode` uses it: the toolbar is an overlay whose
        ancestors may not be shown yet."""
        editing = not self.edit_toolbar.isHidden()
        stage_view = self.stage_controller.get_stage_view()
        self.fixture_inspector.set_instance(instance if editing else None)
        self.fixture_inspector.setVisible(editing and instance is not None)
        if editing and instance is not None:
            self.fixture_inspector.adjustSize()
        stage_view.reposition_overlays()

    def _patch_ui_signature(self):
        """What the timeline / clip library / DMX monitor depend on (per fixture).
        Excludes x/y so a stage drag-move doesn't churn the UI. `mode` is in:
        it changes the channel count, and with it the valid note range."""
        return tuple((f.type, f.mode, f.dmx_start, f.channel, f.name)
                     for f in self.config.fixtures)

    def _on_patch_changed(self):
        """Persist a fixture-patch edit and propagate it live: rebuild the
        timeline tracks, refresh the clip library + DMX monitor, and hot-swap the
        running software DMX engine — each gated by a cheap signature so stage
        drag-moves (and, for the engine, renames) don't cause needless churn.
        The engine's half, signature included, is AppCore.on_patch_edited."""
        self.save_config()

        ui_sig = self._patch_ui_signature()
        if ui_sig != self._last_ui_sig:
            self._last_ui_sig = ui_sig
            self.midi_manager.update_fixtures(self.config.fixtures)
            self._rebuild_patch_views()

        self.app_core.on_patch_edited()
        self.toolbar_manager.update_show_stats()

    def _rebuild_patch_views(self):
        """Rebuild every view laid out from the patch: the timeline lanes, the
        clip library and the DMX monitor."""
        self.timeline.rebuild_for_patch(self.config.fixtures)
        self._refresh_clip_library()
        self.dmx_display.refresh_configuration(self.config)

    def _refresh_clip_library(self):
        """Rebuild the library panel, after a patch edit or a write to a
        fixture type's scene banks or chases."""
        self.clip_library.refresh_configuration(self.config)

    # ------------------------------------------------------------------
    # Show project operations
    # ------------------------------------------------------------------
    def _on_patch_reloaded(self):
        """Widget half of core.reload_from_show: rebuild every patch-dependent
        view (the core already propagated the patch to midi/controller).
        Reuses the exact propagation path of _on_patch_changed, unconditionally."""
        self._last_ui_sig = self._patch_ui_signature()
        self._rebuild_patch_views()
        # The stage too: opening a show rebinds config.fixtures to a new list,
        # and without this the previous show's rig stayed lit until a restart.
        self.stage_controller.sync_from_config(self.config)

    def _on_show_reloaded(self):
        """Counters, show name, autosave chip and title after a show reload."""
        self.playlist_controller.update_song_counter()
        self._refresh_show_status()
        self.update_window_title()

    def _refresh_show_status(self):
        """The top bar's show name, and its autosave chip back at "saved"."""
        show = self.show_manager.show
        self.toolbar_manager.set_show_name(show.name if show else "")
        self.toolbar_manager.set_autosave_state("saved")

    def new_show_dialog(self):
        """Create a new show project (seeded with the default patch) and open
        it. Favourites start empty — they are starred per show."""
        name, ok = QInputDialog.getText(self, "New show", "Show name:")
        if not ok or not name.strip():
            return
        parent = QFileDialog.getExistingDirectory(
            self, "Choose where to create the show", str(default_shows_dir()))
        if not parent:
            return
        self.stop_playback()
        try:
            show = show_store.create_show(
                parent, name.strip(), fixtures=shipped_fixtures())
            self.show_manager.open_show(show.root)
        except ShowError as e:
            ErrorDialogManager.show_validation_error(
                self, "New show", str(e), "a name that doesn't exist there yet")

    def open_show_dialog(self):
        """Open an existing show folder."""
        path = QFileDialog.getExistingDirectory(
            self, "Open show (a .show folder)", str(default_shows_dir()))
        if not path:
            return
        if not show_store.is_show_dir(path):
            ErrorDialogManager.show_validation_error(
                self, "Open show", path, "a folder containing show.json")
            return
        self.stop_playback()
        try:
            self.show_manager.open_show(path)
        except ShowError as e:
            ErrorDialogManager.show_media_file_error(self, path, str(e))

    def save_show_as_dialog(self):
        """Duplicate the show (without its versions/ history) and open the copy."""
        show = self.show_manager.show
        if show is None:
            return
        name, ok = QInputDialog.getText(
            self, "Save show as", "New show name:", text=show.name)
        if not ok or not name.strip():
            return
        parent = QFileDialog.getExistingDirectory(
            self, "Choose where to save the copy", str(show.root.parent))
        if not parent:
            return
        self.stop_playback()
        self.show_manager.flush("save-as")
        try:
            dest = show_store.duplicate_show(show, parent, name.strip())
            self.show_manager.open_show(dest)
        except ShowError as e:
            ErrorDialogManager.show_validation_error(
                self, "Save show as", str(e), "a name that doesn't exist there yet")

    def export_bundle_dialog(self):
        """Export the whole show as one portable .showpack (collect & save)."""
        show = self.show_manager.show
        if show is None:
            return
        from core.show import bundle
        path, _ = QFileDialog.getSaveFileName(
            self, "Export show bundle", f"{show.name}{bundle.BUNDLE_SUFFIX}",
            f"Show bundles (*{bundle.BUNDLE_SUFFIX})")
        if not path:
            return
        if not path.endswith(bundle.BUNDLE_SUFFIX):
            path += bundle.BUNDLE_SUFFIX
        self.show_manager.flush("export-bundle")
        try:
            warnings = bundle.export_bundle(show, path)
        except Exception as e:
            ErrorDialogManager.show_media_file_error(self, path, f"Export failed: {e}")
            return
        message = f"Show bundle exported to:\n{path}"
        if warnings:
            message += "\n\nWarnings:\n" + "\n".join(warnings)
        QMessageBox.information(self, "Export bundle", message)

    def import_bundle_dialog(self):
        """Import a .showpack: pick the file, pick where to unpack, open it."""
        from core.show import bundle
        path, _ = QFileDialog.getOpenFileName(
            self, "Import show bundle", str(default_shows_dir()),
            f"Show bundles (*{bundle.BUNDLE_SUFFIX});;All files (*)")
        if not path:
            return
        dest = QFileDialog.getExistingDirectory(
            self, "Choose where to unpack the show", str(default_shows_dir()))
        if not dest:
            return
        self.stop_playback()
        try:
            root = bundle.import_bundle(path, dest)
            self.show_manager.open_show(root)
        except ShowError as e:
            ErrorDialogManager.show_media_file_error(self, path, str(e))

    def on_song_title_edited(self, title: str):
        """Rename the current song (the core persists it and tells the web
        remote), then refresh the setlist combobox so the renamed entry
        updates in place."""
        if self.app_core.set_song_title(title):
            self.playlist_controller.update_song_counter()

    def on_marker_edit_requested(self, kind: str, time_sec: float):
        """A ruler flag was double-clicked: its editor, opened on it."""
        self.show_markers_dialog(select=(kind, time_sec))

    def show_markers_dialog(self, select=None):
        """Open the per-song marker editor (timeline toolbar ▸ Markers).

        `select` is the (kind, time) of the marker to open on, as a ruler flag
        names it; the toolbar button passes none and lands on the first."""
        song = self.config.get_current_song()
        if song is None:
            return
        songs = self.config.playlist.songs
        dialog = MarkersDialog(
            song.voice_tts, song.control_markers, self,
            audio_engine=self.audio_engine,
            tts_voice=self.config.audio.tts_voice,
            segue=song.segue, segue_time=song.segue_time,
            base_bpm=self.bpm_state.get_bpm(),
            duration=song_duration(song) or self.midi_manager.get_duration() or 0.0,
            song_title=song.title,
            has_next_song=(self.config.playlist.current_song_index
                           < len(songs) - 1),
            select=select)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        song.voice_tts = dialog.voice_tts()
        song.control_markers = dialog.control_markers()
        song.segue = dialog.segue_enabled()
        song.segue_time = dialog.segue_time()
        self.apply_marker_edits(song)

    def on_marker_moved(self, kind: str, old_time: float, new_time: float):
        """A ruler marker was dragged: retime it on the song and re-apply.

        The ruler hands back the kind and the time the flag sat at, which is
        what identifies the marker — the flags are drawn from tuples, and the
        segue is not in any list at all."""
        song = self.config.get_current_song()
        if song is None:
            return
        if kind == core_markers.KIND_SEGUE:
            song.segue_time = new_time
        elif kind == core_markers.KIND_VOICE:
            cue = core_markers.marker_at(song.voice_tts, old_time)
            if cue is None:
                return
            cue.time = new_time
        else:
            marker = core_markers.marker_at(song.control_markers, old_time, kind)
            if marker is None:
                return
            marker.time = new_time
        self.apply_marker_edits(song)

    def apply_marker_edits(self, song):
        """Persist and re-apply a song's marker track after an edit — the Cues
        dialog's OK and a ruler drag both land here. Ruler markers are the
        window's half; the PCM render and the transport half (tempo map, marker
        firing, segue) ride the core."""
        self.save_config()
        self.refresh_timeline_markers(song)
        self.app_core.apply_voice_tts(song)
        self.app_core.apply_song_markers(song)

    def refresh_timeline_markers(self, song):
        """Redraw the ruler's marker track for `song` (also used on load)."""
        self.timeline.set_voice_tts([(c.time, c.text) for c in song.voice_tts])
        self.timeline.set_control_markers(core_markers.ruler_rows(song))

    def take_snapshot_dialog(self):
        """Take a named, pinned (never-pruned) snapshot of the show."""
        show = self.show_manager.show
        if show is None:
            return
        name, ok = QInputDialog.getText(self, "Snapshot", "Snapshot name:")
        if not ok or not name.strip():
            return
        self.show_manager.flush("snapshot")  # capture the very latest edits
        try:
            from core.show import versions
            versions.take_snapshot(show, pinned=True, name=name.strip())
        except Exception as e:
            ErrorDialogManager.show_validation_error(
                self, "Snapshot", str(e), "a writable show folder")
            return
        self._refresh_versions_ui()

    def _refresh_versions_ui(self):
        """Repopulate the ☰ menu's Restore submenu from disk."""
        show = self.show_manager.show
        if show is None:
            self.toolbar_manager.set_versions([])
            return
        from core.show import versions
        self.toolbar_manager.set_versions(versions.list_versions(show))

    def restore_version_from_toolbar(self, version_id: str):
        """Restore the snapshot picked in the ☰ menu's Restore submenu,
        confirming first (the rollback itself snapshots the current state, so
        it stays undoable from History)."""
        if self.show_manager.show is None or not version_id:
            return
        reply = QMessageBox.question(
            self, "Restore version",
            "Restore the show to the selected snapshot?\n\n"
            "The current state is snapshotted first, so this can be undone "
            "from History.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self.show_manager.restore_version(version_id)
        except Exception as e:
            QMessageBox.critical(self, "Restore failed", str(e))

    def on_track_heights_changed(self, waveform_height, midi_events_height):
        """Persist track heights when the user finishes resizing."""
        self.config.window.waveform_track_height = int(waveform_height)
        self.config.window.midi_events_track_height = int(midi_events_height)
        self.save_config()

    def on_header_width_changed(self, width):
        """Persist the timeline's track-header width when the user finishes
        dragging its divider."""
        self.config.window.timeline_header_width = int(width)
        self.save_config()

    def on_track_order_changed(self, channels):
        """Persist the track strip's order after a lane was dragged to a new row.

        The row rides the patch entry (show data, like the fixture's address and
        its rigging), so every fixture is stamped rather than only the moved
        one — a half-filled order would leave the untouched lanes falling back
        to DMX address and reshuffling behind the one that moved."""
        by_channel = {f.channel: f for f in self.config.fixtures}
        for row, channel in enumerate(channels):
            instance = by_channel.get(channel)
            if instance is not None:
                instance.order = row
        self.save_config()

    def _on_row_height_preset(self, height):
        """Apply an M/L fixture-track height preset: expand every (possibly
        collapsed) lane, set the shared height, then grow/shrink the timeline
        section so every track is visible without scrolling."""
        self.timeline.set_all_tracks_collapsed(False)
        self.config.window.tracks_collapsed = False
        self.timeline.set_midi_events_height(height)
        self._fit_timeline_section_to_content()

    def _on_collapse_all_tracks(self):
        """The Rows 'S' preset collapses every fixture lane to a single header
        row, then shrinks the timeline section to fit."""
        self.timeline.set_all_tracks_collapsed(True)
        self.config.window.tracks_collapsed = True
        self._fit_timeline_section_to_content()

    def _apply_persisted_section_height(self) -> None:
        """Restore the timeline section to its persisted height using the
        splitter's actual height (which excludes the top toolbar), so the
        section isn't scaled down by the toolbar's height on load."""
        avail = self.vertical_splitter.height()
        if avail <= 0:
            return
        section = self.config.window.timeline_section_height
        self.vertical_splitter.setSizes([max(160, avail - section), section])

    def _fit_timeline_section_to_content(self):
        """Resize the timeline section to fit its content, clamped so the stage
        area above keeps a minimum height."""
        needed = self.timeline_toolbar.sizeHint().height() + self.timeline.viewport_height_hint()
        sizes = self.vertical_splitter.sizes()
        total = sum(sizes)
        top_min = 200  # keep the stage above visible
        section = max(160, min(needed, total - top_min))
        self.vertical_splitter.setSizes([total - section, section])
        self.config.window.timeline_section_height = int(section)
        self.save_config()

    def _on_vertical_splitter_moved(self, _pos, _index):
        """Track the timeline section height as the divider is dragged.

        Updated in-memory only (the drag fires continuously); it is flushed to
        disk by the existing save_config() calls and on closeEvent.
        """
        sizes = self.vertical_splitter.sizes()
        if len(sizes) == 2:
            self.config.window.timeline_section_height = int(sizes[1])


    def _on_sidebar_splitter_moved(self, _pos, _index):
        """Track the sidebar's width as its divider is dragged.

        Dragging it shut is the collapse gesture, not a width — what is
        persisted is the width to reopen at, so a zero is ignored. In-memory
        only, like the timeline section height above.
        """
        sizes = self._sidebar_splitter.sizes()
        if len(sizes) == 2 and sizes[1] > 0:
            self.config.window.sidebar_width = int(sizes[1])

    def resizeEvent(self, event):
        """Track the window size so it reopens at the size it was left.

        A maximised or full-screen size is deliberately not recorded: restored
        as a plain window it gives one that covers the screen without being
        maximised, and loses the size the user actually chose.
        """
        super().resizeEvent(event)
        if not self.isMaximized() and not self.isFullScreen():
            self.config.window.width = event.size().width()
            self.config.window.height = event.size().height()

    def on_midi_data_changed_from_manager(self):
        """Handle when MIDI data is modified via MidiManager."""
        self.logger.info("MIDI data has been modified via MidiManager")

        # Defer the timeline refresh — this signal often fires
        # synchronously from inside a DraggableMidiEvent's
        # mouseReleaseEvent (via MidiManager.move_event → data_changed).
        # A synchronous refresh would destroy scene items while Qt is
        # still processing events on them, causing SIGSEGV.
        self.timeline._schedule_refresh()

        # Enable export button if we have MIDI data
        has_midi_data = self.midi_manager.midi_data is not None
        self.toolbar_manager.set_export_midi_enabled(has_midi_data)

    def open_file_dialog(self, file_type):
        """Handle file dialog for loading MIDI/Audio files."""
        if file_type == 'midi':
            title = "Select MIDI file"
            filt = "MIDI files (*.mid)"
        elif file_type == 'audio':
            title = "Select Audio file"
            filt = "Audio files (*.mp3 *.wav);;MP3 files (*.mp3);;WAV files (*.wav);;All files (*)"
        elif file_type == 'guide':
            title = "Select Guide track (full mix)"
            filt = "Audio files (*.mp3 *.wav);;MP3 files (*.mp3);;WAV files (*.wav);;All files (*)"
        else:
            title = f"Select {file_type.upper()} file"
            filt = "All files (*)"
        path, _ = QFileDialog.getOpenFileName(self, title, "", filt)
        if path:
            self.load_media_file(file_type, path)

    def load_media_file(self, file_type, path):
        """Load `path` as the current song's backing ('audio'), 'guide' or 'midi'.

        The picker's other half: everything the file dialog does once a path is
        in hand, so a dropped file takes exactly the same route."""
        try:
            # Validate file exists and is accessible
            if not os.path.exists(path):
                ErrorDialogManager.show_media_file_error(self, path, "File not found")
                return
            
            if file_type == 'audio':
                # Validate file extension
                if not is_audio_path(path):
                    ErrorDialogManager.show_media_file_error(self, path, "Unsupported audio format. Please select an MP3 or WAV file.")
                    return
                
                # Backing track: sets it on the CURRENT song, replacing
                # whatever it had — exactly like the guide. Growing the
                # setlist is SETLIST ▸ + alone, so picking a file never
                # silently spawns a song beside the one being edited.
                self.stop_playback()
                song = self.config.get_current_song()
                if song is None:
                    new_index = self.config.add_song(audio=path)
                    self.config.set_current_song_index(new_index)
                    song = self.config.get_current_song()
                    self.logger.info(
                        f"Added first song to the setlist at index {new_index}")
                else:
                    self.config.update_current_song(audio=path, offset=0.0)
                self.playlist_controller.apply_song_config(song)
                self.playlist_controller.update_song_counter()
                self.save_config()
                self.logger.info(f"Backing track set: {path}")
                
            elif file_type == 'guide':
                # Guide track: attaches to the CURRENT song — or starts one
                # of its own when the setlist is empty (a guide is as good
                # a reason for a song to exist as a backing).
                if not is_audio_path(path):
                    ErrorDialogManager.show_media_file_error(self, path, "Unsupported audio format. Please select an MP3 or WAV file.")
                    return
                song = self.config.get_current_song()
                if song is None:
                    self.stop_playback()
                    new_index = self.config.add_song(guide=path)
                    self.config.set_current_song_index(new_index)
                    song = self.config.get_current_song()
                    self.logger.info(
                        f"Added guide-only song to playlist at index {new_index}")
                else:
                    # A different file: its sync offset is not this one's.
                    self.config.update_current_song(guide=path,
                                                    guide_offset=0.0)
                # Full reload keeps engine/waveform/toolbar consistent
                self.playlist_controller.apply_song_config(song)
                self.playlist_controller.update_song_counter()
                self.save_config()
                self.logger.info(f"Guide track set: {path}")

            elif file_type == 'midi':
                # Save pending edits of the current MIDI, then copy the
                # picked file into the show's midis/ — the show owns its
                # sequences; the user's original stays untouched.
                self.show_manager.flush("load-midi")
                owned = self.show_manager.import_midi_copy(path)
                load_path = owned or path
                self.config.update_current_song(midi=load_path)
                self.save_config()

                # Load MIDI file via MidiManager and update display
                self.show_manager.begin_song_load()
                try:
                    loaded = self.midi_manager.load_from_file(load_path)
                finally:
                    self.show_manager.end_song_load()
                if loaded:
                    self.timeline.refresh_from_manager(detect_bpm=True)
                    self.toolbar_manager.update_midi_file_display(load_path)
                    self.toolbar_manager.set_export_midi_enabled(True)
                else:
                    ErrorDialogManager.show_media_file_error(self, path, "Failed to load MIDI file")
                    return
            
            self.update_window_title()
                
        except PermissionError:
            ErrorDialogManager.show_file_permission_error(self, path, "read")
        except Exception as e:
            ErrorDialogManager.show_media_file_error(self, path, str(e))

    def dragEnterEvent(self, event):
        if audio_paths_from_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        # Same answer as the enter: a child that refuses the drag (the timeline
        # takes clips and scenes, not files) propagates its moves up here too.
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        """Take a dropped audio file as the current song's backing or guide."""
        paths = audio_paths_from_mime(event.mimeData())
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        if len(paths) > 1:
            self.logger.info(
                f"Dropped {len(paths)} audio files, loading the first one: "
                f"{paths[0]}")
        # Ask AFTER the drop returns: a modal dialog opened while the drag
        # session is still unwinding keeps the pointer grabbed.
        QTimer.singleShot(0, lambda path=paths[0]: self._load_dropped_audio(path))

    def _load_dropped_audio(self, path):
        """Which of the two audio slots did the user mean? Only they know."""
        box = QMessageBox(self)
        box.setWindowTitle("Load audio file")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"Load “{os.path.basename(path)}” as:")
        box.setInformativeText(
            "The backing track is what plays live; the guide track is a "
            "full-mix reference for designing to.")
        backing = box.addButton("Backing track",
                                QMessageBox.ButtonRole.AcceptRole)
        guide = box.addButton("Guide track", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(backing)
        box.exec()
        clicked = box.clickedButton()
        if clicked is backing:
            self.load_media_file('audio', path)
        elif clicked is guide:
            self.load_media_file('guide', path)

    def clear_backing_track(self):
        """Remove the current song's backing track (the file stays on disk).

        The SONG survives: its guide, MIDI, cues and offsets stay put and it
        keeps its place in the setlist — removing a song is the SETLIST
        panel's 🗑 button."""
        song = self.config.get_current_song()
        if song is None or not song.audio:
            return
        self.config.update_current_song(audio="", offset=0.0)
        self.save_config()
        self.playlist_controller.apply_song_config(song)
        self.logger.info("Cleared backing track")

    def clear_guide_track(self):
        """Remove the current song's guide track (the file stays on disk)."""
        song = self.config.get_current_song()
        if song is None or not song.guide:
            return
        self.config.update_current_song(guide="", guide_offset=0.0)
        self.save_config()
        self.playlist_controller.apply_song_config(song)
        self.logger.info("Cleared guide track")

    def create_new_midi_track(self):
        """Handle creating a new empty MIDI track."""
        # Create empty MIDI track via MidiManager
        self.midi_manager.create_empty_track()
        self.timeline.refresh_from_manager()
        
        # Clear the MIDI file path in current song
        self.config.update_current_song(midi="")
        self.toolbar_manager.update_midi_file_display("New MIDI track")
        
        # Enable export button since we now have MIDI data
        self.toolbar_manager.set_export_midi_enabled(True)
        
        self.save_config()
        self.logger.info("Created new empty MIDI track")

    def export_midi_dialog(self):
        """Handle export MIDI file dialog."""
        if not self.midi_manager.midi_data:
            ErrorDialogManager.show_validation_error(
                self, "Export MIDI", "No MIDI data available", "Load a MIDI file or create events first"
            )
            return
        
        title = "Export MIDI file"
        filt = "MIDI files (*.mid)"
        suggested_name = ""
        
        # Suggest name based on current MIDI file or use default
        current_song = self.config.get_current_song()
        if current_song and current_song.midi:
            base_name = os.path.splitext(os.path.basename(current_song.midi))[0]
            suggested_name = f"{base_name}_export.mid"
        else:
            suggested_name = "exported_midi.mid"
        
        path, _ = QFileDialog.getSaveFileName(self, title, suggested_name, filt)
        if path:
            try:
                # Ensure .mid extension
                if not path.lower().endswith('.mid'):
                    path += '.mid'
                
                # Export via MidiManager — interop: a clean standard-MIDI
                # copy without the tdmx1 authoring metas (saving the show is
                # the autosave's job, not this button's).
                if self.midi_manager.export_to_file(path, self.bpm_state.get_bpm(),
                                                    interop=True):
                    self.logger.info(f"MIDI file exported successfully to: {path}")
                    QMessageBox.information(self, "Export Success", f"MIDI file exported to:\n{path}")
                else:
                    ErrorDialogManager.show_validation_error(
                        self, "Export Failed", "Failed to export MIDI file", "Check the file path and permissions"
                    )
                    
            except PermissionError:
                ErrorDialogManager.show_file_permission_error(self, path, "write")
            except Exception as e:
                ErrorDialogManager.show_media_file_error(self, path, f"Export failed: {str(e)}")

    def save_config(self):
        """Single persistence funnel (prefs + show manifest) — in the core."""
        self.app_core.save_config()

    def _on_config_save_failed(self, prefs_file):
        ErrorDialogManager.show_file_permission_error(self, prefs_file, 'save')

    def play_pause_playback(self):
        """Toggle playback via the core."""
        self.app_core.play_pause_playback()

    def stop_playback(self):
        """Stop playback via the core and reset the playhead."""
        self.app_core.stop_playback()
        self.timeline.update_playhead(0)

    def seek_playback(self, time_seconds):
        """Seek to position via the core (which resyncs the metronome)."""
        self.app_core.seek_playback(time_seconds)

    def update_window_title(self):
        """Update window title with the open show and loaded file names."""
        title = "TupperDMX Simulator"
        show = self.show_manager.show
        if show is not None:
            title += f" — {show.name}.show"
        current_song = self.config.get_current_song()
        if current_song and current_song.audio:
            file_name = os.path.basename(current_song.audio)
            title += f" - {file_name}"
        self.setWindowTitle(title)

    def _spacebar_toggles_playback(self):
        """True when Space means play/pause instead of a keystroke for a widget.

        The filter is installed on the QApplication, so it sees every key press
        in the process: while a dialog is up, or a text field in this window has
        focus, Space belongs to them and must not reach the transport.
        """
        return self._window_owns_key(TEXT_ENTRY_WIDGETS)

    def _arrow_seeks_playhead(self):
        """True when ←/→ mean "move the playhead" instead of a keystroke for a
        widget.

        More widgets steer with the arrows than with Space, and this filter is
        installed on the QApplication, so it sees every key press in the
        process: on top of Space's text fields and spin boxes, a slider moves
        its value, an item view its current row, a tab bar its page. The line
        is whether the widget has another way to do the same thing — it does
        not, so it keeps the key, while a closed combo box (the setlist among
        them, and it holds focus at launch) steers with Up/Down just as well
        and hands ←/→ to the transport. An open menu and a dialog own the key
        outright, which the popup and active-window checks answer.
        """
        if QApplication.activePopupWidget() is not None:
            return False
        return self._window_owns_key(
            TEXT_ENTRY_WIDGETS + (QAbstractSlider, QAbstractItemView, QTabBar))

    def _window_owns_key(self, keepers):
        """True unless the key belongs elsewhere: to another window that is
        active (a dialog), or to the focused widget — one of the `keepers`
        types, or an editable combo box, whose text caret it moves."""
        active = QApplication.activeWindow()
        if active is not None and active is not self:
            return False
        focus = QApplication.focusWidget()
        if isinstance(focus, keepers):
            return False
        return not (isinstance(focus, QComboBox) and focus.isEditable())

    def _arrow_seek(self, direction, new_hold):
        """Move the playhead one step: `direction` -1 for ←, +1 for →.

        The step grows with how long the key has been held, so a tap nudges and
        a hold sweeps. Auto-repeat drives the hold, but ARROW_SEEK_INTERVAL
        paces it — and a fresh press skips that throttle, so a tap always
        lands. Pressing the other key is a fresh press, which is what restarts
        the ramp on a direction change.

        The upper bound is the song's own length (its longest audio track, else
        its sequence), the same one the web remote's scrubber takes; a song
        with neither is unbounded, since nothing measures it.
        """
        now = time.monotonic()
        if new_hold:
            self._arrow_held_since = now
        elif now - self._arrow_last_step < ARROW_SEEK_INTERVAL:
            return
        self._arrow_last_step = now

        ramp = min((now - self._arrow_held_since) / ARROW_SEEK_RAMP, 1.0)
        step = ARROW_SEEK_STEP + (ARROW_SEEK_MAX_STEP - ARROW_SEEK_STEP) * ramp
        target = max(0.0, self.media_controller.get_current_position()
                     + direction * step)
        end = (song_duration(self.config.get_current_song())
               or self.midi_manager.get_duration() or 0.0)
        if end:
            target = min(target, end)

        self.seek_playback(target)
        # A seek while paused emits no position_changed (the tick returns early
        # when the transport is stopped), so move the playhead and the clock
        # here — watching them move is the whole point of the key.
        self._on_position_changed(target)

    def _escape_leaves_edit_mode(self):
        """True when Esc means "back out of the stage designer".

        Every dialog cancels on Esc for free (QDialog routes it to reject()), and
        so do combo/menu popups — so this filter, which sees every key press in
        the process, must stand down while one of those is up and answer only for
        the designer, which is a mode rather than a window. Modality is asked
        about directly and not left to `activeWindow`, because the Add-fixture
        dialog opens FROM the designer's own toolbar: its Esc arrives with edit
        mode still on, and eating it would strand the dialog.

        The stage window counts as ours — the stage is designable undocked too,
        and the toolbar rides the view into it. `isHidden` rather than
        `isVisible` because the toolbar is an overlay whose ancestors may not be
        up yet.
        """
        if self.edit_toolbar.isHidden():
            return False
        if (QApplication.activePopupWidget() is not None
                or QApplication.activeModalWidget() is not None):
            return False
        active = QApplication.activeWindow()
        return active is None or active is self or active is self._stage_window

    def _leave_edit_level(self):
        """Esc backs out ONE level: the inspector first, then edit mode itself.

        Clearing the selection is what the inspector's own ✕ does — the card
        hides in response to `fixture_selected(None)`, never on its own.
        """
        if not self.fixture_inspector.isHidden():
            self.stage_controller.scene.clearSelection()
            return
        self.exit_edit_mode()

    def eventFilter(self, source, event):
        """Global event filter to capture spacebar / Esc / ←→ from any widget.

        Installed on the QApplication, so it runs for EVERY event on the GUI
        thread — paints, timers, queued signals. The KeyPress test stays the
        one gate in front of everything else: only a key event has `.key()`,
        and an exception raised in here aborts the whole process (PyQt6's
        qFatal on an unhandled exception in a virtual), on the next event.
        """
        if event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Space and self._spacebar_toggles_playback():
                self.play_pause_playback()
                return True  # Event handled, don't pass to widget
            if key == Qt.Key.Key_Escape and self._escape_leaves_edit_mode():
                self._leave_edit_level()
                return True
            if (key in (Qt.Key.Key_Left, Qt.Key.Key_Right)
                    and self._arrow_seeks_playhead()):
                self._arrow_seek(1 if key == Qt.Key.Key_Right else -1,
                                 new_hold=not event.isAutoRepeat())
                return True
        return super().eventFilter(source, event)

    def closeEvent(self, event):
        """Handle application close event."""
        self.logger.info("Application closing - cleaning up resources")

        # Remove global event filter
        QApplication.instance().removeEventFilter(self)

        # A floating stage is a second top-level window: left open it keeps the
        # app alive after the main window is gone.
        if self._stage_window is not None:
            self.dock_stage()

        # Flush the show first, clean up every component, save config — all
        # in the core (idempotent, shared with headless shutdown).
        self.app_core.shutdown()

        # Accept the close event
        event.accept()
