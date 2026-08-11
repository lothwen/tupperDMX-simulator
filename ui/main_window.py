# ABOUTME: MainWindow — the desktop view over app.core.AppCore: builds the
# ABOUTME: widget tree and wires the widget halves of the core's signals. The
# ABOUTME: thin entry point lives in main.py.
import os
from copy import deepcopy
from dataclasses import asdict

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QPushButton, QFileDialog, QSplitter, QMessageBox,
                             QStyle, QTabWidget, QDialog, QInputDialog,
                             QAbstractSpinBox, QComboBox, QLineEdit,
                             QPlainTextEdit, QTextEdit)
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
from timeline import Timeline
from tupperlogger import get_logger
from ui.widgets import icons
from ui.about_dialog import AboutDialog
from ui.clip_library import ClipLibraryWidget
from ui.dmx_display import DMXDisplayWidget
from ui.error_dialogs import ErrorDialogManager
from ui.fixture.designer import StageEditToolbar, FixtureInspector
from ui.playlist_controller import PlaylistController
from ui.settings_dialog import SettingsDialog
from ui.stage_window import StageWindow
from stage.controller import StageController
from ui.timeline_toolbar import TimelineToolbar
from ui.toolbar_manager import ToolbarManager
from ui.voice_cues_dialog import VoiceCuesDialog


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
        self.setMinimumSize(1200, 800)
        self.setGeometry(100, 100, self.config.window.width, self.config.window.height)

        # Setup main layout
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QVBoxLayout(self.central_widget)
        self.main_layout.setContentsMargins(0, 0, 0, 0)  # Remove all margins
        self.main_layout.setSpacing(0)  # Remove spacing between widgets

        # Create UI
        self.setup_ui()

        # Live-patch baselines: the last-applied patch signatures, so
        # _on_patch_changed only propagates real changes.
        self._last_ui_sig = self._patch_ui_signature()
        self._last_dmx_sig = self._patch_dmx_signature()

        # Widget fan-out of BPM changes (the metronome/count-in half already
        # rides bpm_state inside the core). Connected before the initial song
        # load, which is gated by begin/end_song_load like any other load.
        self.bpm_state.bpm_changed.connect(self._on_bpm_state_changed)

        # Bring the core up: current song (or empty state), persisted I/O
        # routing, web remote.
        self.app_core.start()

        # Update song counter in toolbar
        self.playlist_controller.update_song_counter()

        # Install global event filter for spacebar handling
        QApplication.instance().installEventFilter(self)

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
        self.clip_library = ClipLibraryWidget(self.config)
        self.clip_library.preview_requested.connect(self.app_core.preview_clip)

        self.audio_controller.audio_error_occurred.connect(self._on_audio_error)

        self.stage_controller = StageController(self.config)

        self.toolbar_manager = ToolbarManager(self.config)
        # Transport lives in a toolbar above the timeline (built in setup_ui).
        self.timeline_toolbar = TimelineToolbar(play_icon, pause_icon, stop_icon)
        self._connect_toolbar_signals()

    def _connect_toolbar_signals(self):
        """Connect toolbar manager signals to appropriate handlers."""
        self.toolbar_manager.load_file_requested.connect(self.open_file_dialog)
        self.toolbar_manager.guide_clear_requested.connect(self.clear_guide_track)
        self.toolbar_manager.new_midi_track_requested.connect(self.create_new_midi_track)
        self.toolbar_manager.export_midi_requested.connect(self.export_midi_dialog)
        # Show project signals
        self.toolbar_manager.new_show_requested.connect(self.new_show_dialog)
        self.toolbar_manager.open_show_requested.connect(self.open_show_dialog)
        self.toolbar_manager.save_show_requested.connect(self.save_show_now)
        self.toolbar_manager.save_show_as_requested.connect(self.save_show_as_dialog)
        self.toolbar_manager.snapshot_requested.connect(self.take_snapshot_dialog)
        self.toolbar_manager.restore_version_requested.connect(self.restore_version_from_toolbar)
        self.toolbar_manager.export_bundle_requested.connect(self.export_bundle_dialog)
        self.toolbar_manager.import_bundle_requested.connect(self.import_bundle_dialog)
        self.toolbar_manager.song_title_edited.connect(self.on_song_title_edited)
        self.toolbar_manager.song_key_edited.connect(self.on_song_key_edited)
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
        self.timeline_toolbar.monitor_changed.connect(
            self.media_controller.set_monitor)
        self.media_controller.monitor_changed.connect(self._on_monitor_changed)
        # Playlist navigation signals
        self.toolbar_manager.prev_song_requested.connect(self.playlist_controller.prev_song)
        self.toolbar_manager.next_song_requested.connect(self.playlist_controller.next_song)
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
        show = self.show_manager.show
        self.toolbar_manager.set_show_name(show.name if show else "")
        self.toolbar_manager.set_autosave_state("saved")
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
            fixtures=self.config.fixtures,
        )
        self.timeline.seek_requested.connect(self.seek_playback)
        self.timeline.bpm_detected.connect(self.on_bpm_detected_from_timeline)
        self.timeline.waveform_offset_changed.connect(self.on_waveform_offset_changed)
        self.timeline.metronome_volume_changed.connect(self.on_metronome_volume_changed)
        self.timeline.metronome_mute_toggled.connect(self.on_metronome_mute_toggled)
        self.timeline.audio_volume_changed.connect(self.on_audio_volume_changed)
        self.timeline.audio_mute_toggled.connect(self.on_audio_mute_toggled)
        self.timeline.voice_volume_changed.connect(self.on_voice_volume_changed)
        self.timeline.voice_mute_toggled.connect(self.on_voice_mute_toggled)
        self.timeline.track_heights_changed.connect(self.on_track_heights_changed)
        # The clip editor can write a fixture's scene banks, which the library
        # panel has already read — rebuild it rather than wait for a restart.
        self.timeline.library_changed.connect(
            lambda: self.clip_library.refresh_configuration(self.config))
        # Auditioning a clip from the editor takes the same route as the clip
        # library's click-to-preview, so config.io routing applies.
        self.timeline.preview_requested.connect(self.app_core.preview_clip)
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
        self.timeline_toolbar.voice_cues_requested.connect(self.show_voice_cues_dialog)
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

        self.main_content.setSizes([self.width() - 250, 250])

        # Floating toggle button for the DMX side panel
        self._dmx_panel_collapsed = False
        self._last_dmx_width = 250
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
            self.stage_controller, self._on_patch_changed, self.exit_edit_mode)
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
        """Handle waveform offset changes from timeline: the drag edits the
        offset of whichever track is being monitored."""
        mc = self.media_controller
        if mc.monitor == MONITOR_GUIDE and mc.guide_file:
            mc.set_guide_offset(offset_sec)
            self.config.update_current_song(guide_offset=offset_sec)
        else:
            mc.set_audio_offset(offset_sec)
            self.config.update_current_song(offset=offset_sec)
        self.save_config()

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

    def _on_audio_error(self, error_message, error_type):
        """Handle audio system errors (the core already logged them)."""
        ErrorDialogManager.show_audio_hardware_error(self, error_message, error_type)

    def on_bpm_detected_from_timeline(self, value):
        """Handle BPM detection from timeline analysis."""
        self.bpm_state.set_bpm(value)
    
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
        # Show user-friendly error dialog
        if file_path:
            ErrorDialogManager.show_media_file_error(self, file_path, error_message)
        else:
            # Generic media error
            ErrorDialogManager.show_media_file_error(self, "Unknown file", error_message)

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
        controller = getattr(self, "software_controller", None)
        if controller is not None:
            controller.set_manual_channel(channel, value)

    def update_dmx(self, dmx_data):
        """Update DMX data across all components."""
        # Update stage fixtures
        self.stage_controller.update_fixtures_with_dmx(dmx_data)
        
        # Update DMX display
        self.dmx_display.update_dmx_data(dmx_data)

    def on_metronome_volume_changed(self, value):
        """Handle metronome volume changes from timeline."""
        volume = float(value) / 100.0
        self.audio_controller.set_metronome_volume(volume)
        self.config.update_volume(metronome=volume)
        self.save_config()

    def on_metronome_mute_toggled(self, muted):
        """Handle metronome mute toggle from the timeline header's CLICK row."""
        self.audio_controller.set_metronome_muted(muted)

    def show_settings_dialog(self):
        """Open the preferences editor and apply any changes live."""
        old_io = asdict(self.config.io)
        old_artnet = asdict(self.config.artnet)
        old_webremote = asdict(self.config.webremote)
        old_audio = asdict(self.config.audio)
        old_fog = asdict(self.config.window.fog)

        dialog = SettingsDialog(self.config, self, audio_engine=self.audio_engine)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        # The dialog hands back a NEW fog object, so the light layer is still
        # holding the old one until it is told otherwise.
        if asdict(self.config.window.fog) != old_fog:
            self.stage_controller.apply_stage_config()

        # Re-apply the routing when it (or the Art-Net target the sinks and
        # listener read from) changed.
        if asdict(self.config.io) != old_io or asdict(self.config.artnet) != old_artnet:
            self.app_core.apply_io_config()

        if asdict(self.config.webremote) != old_webremote:
            self.webremote_manager.apply_config()

        if asdict(self.config.audio) != old_audio:
            self.app_core.apply_audio_config()

        self.save_config()
        self.logger.info("Settings updated")

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
            self._docked_content_sizes or [self.width() - 250, 250])

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

    def _on_fixture_selected(self, instance):
        """Show + populate the inspector for the selected fixture (or hide it)."""
        stage_view = self.stage_controller.get_stage_view()
        self.fixture_inspector.set_instance(instance)
        self.fixture_inspector.setVisible(instance is not None)
        if instance is not None:
            self.fixture_inspector.adjustSize()
        stage_view.reposition_overlays()

    def _patch_ui_signature(self):
        """What the timeline / clip library / DMX monitor depend on (per fixture).
        Excludes x/y so a stage drag-move doesn't churn the UI. `mode` is in:
        it changes the channel count, and with it the valid note range."""
        return tuple((f.type, f.mode, f.dmx_start, f.channel, f.name)
                     for f in self.config.fixtures)

    def _patch_dmx_signature(self):
        """What the software DMX engine depends on (per fixture). Excludes the
        name so a rename never re-patches (and never blacks out) the rig;
        includes `mode`, which changes the fixture's DMX footprint."""
        return tuple((f.type, f.mode, f.dmx_start, f.channel)
                     for f in self.config.fixtures)

    def _on_patch_changed(self):
        """Persist a fixture-patch edit and propagate it live: rebuild the
        timeline tracks, refresh the clip library + DMX monitor, and hot-swap the
        running software DMX engine — each gated by a cheap signature so stage
        drag-moves (and, for the engine, renames) don't cause needless churn."""
        self.save_config()

        ui_sig = self._patch_ui_signature()
        if ui_sig != self._last_ui_sig:
            self._last_ui_sig = ui_sig
            self.midi_manager.update_fixtures(self.config.fixtures)
            self.timeline.rebuild_for_patch(self.config.fixtures)
            self.clip_library.refresh_configuration(self.config)
            self.dmx_display.refresh_configuration(self.config)

        dmx_sig = self._patch_dmx_signature()
        controller = getattr(self, "software_controller", None)
        if controller is not None and dmx_sig != self._last_dmx_sig:
            self._last_dmx_sig = dmx_sig
            self.app_core.repatch_controller()

        self.toolbar_manager.update_show_stats()

    # ------------------------------------------------------------------
    # Show project operations
    # ------------------------------------------------------------------
    def _on_patch_reloaded(self):
        """Widget half of core.reload_from_show: rebuild every patch-dependent
        view (the core already propagated the patch to midi/controller).
        Reuses the exact propagation path of _on_patch_changed, unconditionally."""
        self._last_ui_sig = self._patch_ui_signature()
        self._last_dmx_sig = self._patch_dmx_signature()
        self.timeline.rebuild_for_patch(self.config.fixtures)
        self.clip_library.refresh_configuration(self.config)
        self.dmx_display.refresh_configuration(self.config)
        # The stage too: opening a show rebinds config.fixtures to a new list,
        # and without this the previous show's rig stayed lit until a restart.
        self.stage_controller.sync_from_config(self.config)

    def _on_show_reloaded(self):
        """Counters, show name, autosave chip and title after a show reload."""
        self.playlist_controller.update_song_counter()
        show = self.show_manager.show
        self.toolbar_manager.set_show_name(show.name if show else "")
        self.toolbar_manager.set_autosave_state("saved")
        self.update_window_title()

    def new_show_dialog(self):
        """Create a new show project (seeded with the default patch and this
        machine's default cues) and open it."""
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
                parent, name.strip(),
                fixtures=shipped_fixtures(),
                cues=deepcopy(self.config.webremote.cues))
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

    def save_show_now(self):
        """Manual Save: a forced flush (autosave already covers the rest)."""
        self.show_manager.flush("manual")

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
        """Persist an edited song title (feeds the setlist + web remote)."""
        song = self.config.get_current_song()
        if song is None or song.title == title.strip():
            return
        song.title = title.strip()
        self.save_config()
        # Refresh the setlist combobox so the renamed entry updates in place.
        self.playlist_controller.update_song_counter()
        webremote = getattr(self, "webremote_manager", None)
        if webremote is not None:
            webremote._publish_full()

    def show_voice_cues_dialog(self):
        """Open the per-song voice-cue editor (timeline toolbar 🗣 Cues)."""
        song = self.config.get_current_song()
        if song is None:
            return
        dialog = VoiceCuesDialog(song.voice_cues, self,
                                 audio_engine=self.audio_engine,
                                 tts_voice=self.config.audio.tts_voice)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        song.voice_cues = dialog.cues()
        self.save_config()
        # Ruler markers are the window's half; the PCM render rides the core.
        self.timeline.set_voice_cues([(c.time, c.text) for c in song.voice_cues])
        self.app_core.apply_voice_cues(song)

    def on_song_key_edited(self, key: str):
        """Persist the current song's musical key (TONO chip / web remote)."""
        song = self.config.get_current_song()
        if song is None or song.key == key:
            return
        song.key = key
        self.save_config()
        webremote = getattr(self, "webremote_manager", None)
        if webremote is not None:
            webremote._publish_full()

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
        """Repopulate the toolbar's snapshot picker + counts from disk."""
        show = self.show_manager.show
        if show is None:
            self.toolbar_manager.set_versions([])
            return
        from core.show import versions
        self.toolbar_manager.set_versions(versions.list_versions(show))

    def restore_version_from_toolbar(self, version_id: str):
        """Restore the snapshot picked in the toolbar's VERSIONS combobox,
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

    def on_audio_volume_changed(self, value):
        """Handle audio volume changes from timeline."""
        volume = float(value) / 100.0
        self.media_controller.set_audio_volume(volume)
        self.config.update_volume(audio=volume)
        self.save_config()

    def on_audio_mute_toggled(self, muted):
        """Handle audio mute toggle from timeline."""
        self.media_controller.set_audio_muted(muted)

    def on_voice_volume_changed(self, value):
        """Handle voice (TTS guide) volume changes from timeline."""
        volume = float(value) / 100.0
        self.audio_engine.set_bus_volume(BUS_VOICE, volume)
        self.config.update_volume(voice=volume)
        self.save_config()

    def on_voice_mute_toggled(self, muted):
        """Handle voice mute toggle from the timeline header's VOICE row."""
        self.audio_engine.set_bus_muted(BUS_VOICE, muted)

    def on_track_heights_changed(self, waveform_height, midi_events_height):
        """Persist track heights when the user finishes resizing."""
        self.config.window.waveform_track_height = int(waveform_height)
        self.config.window.midi_events_track_height = int(midi_events_height)
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


    def on_midi_data_changed_from_manager(self):
        """Handle when MIDI data is modified via MidiManager."""
        self.logger.info("MIDI data has been modified via MidiManager")

        # Defer the timeline refresh — this signal often fires
        # synchronously from inside a DraggableMidiEvent's
        # mouseReleaseEvent (via MidiManager.move_event → data_changed).
        # A synchronous refresh would destroy scene items while Qt is
        # still processing events on them, causing SIGSEGV.
        if self.timeline:
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
            try:
                # Validate file exists and is accessible
                if not os.path.exists(path):
                    ErrorDialogManager.show_media_file_error(self, path, "File not found")
                    return
                
                if file_type == 'audio':
                    # Validate file extension
                    file_ext = path.lower().split('.')[-1]
                    if file_ext not in ['mp3', 'wav']:
                        ErrorDialogManager.show_media_file_error(self, path, "Unsupported audio format. Please select an MP3 or WAV file.")
                        return
                    
                    # Check if song already exists in playlist by audio path
                    existing_index = self.config.find_song_by_audio(path)
                    if existing_index is not None:
                        # Switch to existing song
                        self.stop_playback()
                        self.config.set_current_song_index(existing_index)
                        song = self.config.get_current_song()
                        if song:
                            self.playlist_controller.apply_song_config(song)
                        self.logger.info(f"Switched to existing song at index {existing_index}")
                    else:
                        # Add new song to playlist
                        self.stop_playback()
                        new_index = self.config.add_song(audio=path)
                        self.config.set_current_song_index(new_index)
                        song = self.config.get_current_song()
                        if song:
                            self.playlist_controller.apply_song_config(song)
                        self.logger.info(f"Added new song to playlist at index {new_index}")

                    self.playlist_controller.update_song_counter()
                    self.save_config()
                    
                elif file_type == 'guide':
                    # Guide track: attaches to the CURRENT song (never creates
                    # or switches one — the backing stays the song identity).
                    file_ext = path.lower().split('.')[-1]
                    if file_ext not in ['mp3', 'wav']:
                        ErrorDialogManager.show_media_file_error(self, path, "Unsupported audio format. Please select an MP3 or WAV file.")
                        return
                    song = self.config.get_current_song()
                    if song is None:
                        ErrorDialogManager.show_media_file_error(self, path, "No song selected — load a backing track first.")
                        return
                    self.config.update_current_song(guide=path)
                    self.save_config()
                    # Full reload keeps engine/waveform/toolbar consistent
                    self.playlist_controller.apply_song_config(song)
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

    def start_playback(self):
        """Start media playback using media controller."""
        self.media_controller.start_playback()

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
        active = QApplication.activeWindow()
        if active is not None and active is not self:
            return False
        focus = QApplication.focusWidget()
        if isinstance(focus, (QLineEdit, QTextEdit, QPlainTextEdit,
                              QAbstractSpinBox)):
            return False
        return not (isinstance(focus, QComboBox) and focus.isEditable())

    def eventFilter(self, source, event):
        """Global event filter to capture spacebar from any widget."""
        if (event.type() == QEvent.Type.KeyPress and
            event.key() == Qt.Key.Key_Space and
            self._spacebar_toggles_playback()):
            # Toggle play/pause when spacebar is pressed anywhere
            self.play_pause_playback()
            return True  # Event handled, don't pass to widget
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
