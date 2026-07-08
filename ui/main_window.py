# ABOUTME: MainWindow — the central orchestrator wiring managers, UI components
# ABOUTME: and playback state together. The thin entry point lives in main.py.
import os
from dataclasses import asdict

import mido

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QPushButton, QFileDialog, QSplitter, QMessageBox,
                             QStyle, QTabWidget, QDialog)
from PyQt6.QtGui import QShortcut, QKeySequence
from PyQt6.QtCore import (Qt, QTimer, QEvent, QVariantAnimation,
                          QAbstractAnimation, QEasingCurve)

from core import note_layout
from core.config import Config, userprefs_path
from core.io_routing import midi_routing_from_io
from timeline import Timeline
from dmx import DMXManager
from midi import MidiManager
from tupperlogger import get_logger
from ui.about_dialog import AboutDialog
from ui.audio_controller import AudioController
from ui.clip_library import ClipLibraryWidget
from ui.dmx_display import DMXDisplayWidget
from ui.error_dialogs import ErrorDialogManager
from ui.fixture_designer import StageEditToolbar, FixtureInspector
from ui.media_controller import MediaController
from ui.playlist_controller import PlaylistController
from ui.settings_dialog import SettingsDialog
from ui.stage_controller import StageController
from ui.timeline_toolbar import TimelineToolbar
from ui.toolbar_manager import ToolbarManager


class MainWindow(QMainWindow):
    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.logger = get_logger("ui.main")

        # Playlist navigation + per-song load orchestration. Created early
        # (only needs the window ref) so toolbar signal wiring can target it.
        self.playlist_controller = PlaylistController(self)

        # Initialize managers
        self.dmx_manager = DMXManager(self.config)
        self.dmx_manager.dmx_data_received.connect(self.update_dmx)
        self.dmx_manager.connection_status_changed.connect(self.on_dmx_status_changed)
        self.dmx_manager.error_occurred.connect(self.on_dmx_error)
        self.dmx_manager.software_controller_changed.connect(self.on_software_controller_changed)
        
        # The note layout is data-driven (note_layout mirrors the controller's
        # handle_note_on); build_note_map() materialises the standalone-clip
        # notes for the renderer/codec, per-fixture verbs resolve on demand.
        self.midi_manager = MidiManager(note_layout.build_note_map(),
                                        fixtures=self.config.fixtures)
        self.midi_manager.connection_status_changed.connect(self.on_midi_status_changed)
        self.midi_manager.error_occurred.connect(self.on_midi_error)
        self.midi_manager.data_changed.connect(self.on_midi_data_changed_from_manager)
        
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

        # Live-patch baselines: the running software controller (None until a
        # localhost Art-Net worker spawns one) and the last-applied patch
        # signatures, so _on_patch_changed only propagates real changes.
        self.software_controller = None
        self._last_ui_sig = self._patch_ui_signature()
        self._last_dmx_sig = self._patch_dmx_signature()

        # Load current song from playlist
        current_song = self.config.get_current_song()
        if current_song:
            self.playlist_controller.apply_song_config(current_song)
        else:
            # Create empty MIDI track if no songs in playlist
            self.midi_manager.create_empty_track()
            self.timeline.refresh_from_manager()
            self.toolbar_manager.update_midi_file_display("")
            self.toolbar_manager.update_audio_file_display("")

        # Update song counter in toolbar
        self.playlist_controller.update_song_counter()
        
        # Apply the persisted I/O routing (MIDI destinations, controller
        # lifecycle + DMX sinks, stage input).
        self._apply_io_config()
        
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

    def _setup_ui_components(self):
        """Initialize all UI component managers."""
        # Get icons
        play_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        pause_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPause)
        stop_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop)
        
        # Initialize UI components
        self.media_controller = MediaController(self.midi_manager, self.config.volume.audio)
        self.media_controller.playback_state_changed.connect(self._on_playback_state_changed)
        self.media_controller.position_changed.connect(self._on_position_changed)
        self.media_controller.error_occurred.connect(self._on_media_error)
        
        self.dmx_display = DMXDisplayWidget(self.config)
        self.dmx_display.channel_value_edited.connect(self._on_dmx_channel_edited)
        self.clip_library = ClipLibraryWidget(self.config)
        self.clip_library.preview_requested.connect(self._preview_clip)

        self.audio_controller = AudioController(self.config.volume.metronome)
        self.audio_controller.beat_occurred.connect(self._on_metronome_beat)
        self.audio_controller.audio_error_occurred.connect(self._on_audio_error)
        
        self.stage_controller = StageController(self.config)

        self.toolbar_manager = ToolbarManager(self.config)
        # Transport lives in a toolbar above the timeline (built in setup_ui).
        self.timeline_toolbar = TimelineToolbar(play_icon, pause_icon, stop_icon)
        self._connect_toolbar_signals()

    def _connect_toolbar_signals(self):
        """Connect toolbar manager signals to appropriate handlers."""
        self.toolbar_manager.load_file_requested.connect(self.open_file_dialog)
        self.toolbar_manager.new_midi_track_requested.connect(self.create_new_midi_track)
        self.toolbar_manager.export_midi_requested.connect(self.export_midi_dialog)
        # Transport signals now originate from the timeline toolbar.
        self.timeline_toolbar.play_pause_requested.connect(self.play_pause_playback)
        self.timeline_toolbar.stop_requested.connect(self.stop_playback)
        self.timeline_toolbar.bpm_changed.connect(self.bpm_changed)
        # Playlist navigation signals
        self.toolbar_manager.prev_song_requested.connect(self.playlist_controller.prev_song)
        self.toolbar_manager.next_song_requested.connect(self.playlist_controller.next_song)
        self.toolbar_manager.remove_song_requested.connect(self.playlist_controller.remove_current_song)
        # Application menu signals
        self.toolbar_manager.settings_requested.connect(self.show_settings_dialog)
        self.toolbar_manager.about_requested.connect(self.show_about_dialog)
    
    def setup_ui(self):
        """Setup the main UI layout."""
        # Create and add toolbar
        toolbar = self.toolbar_manager.create_toolbar(self)
        self.addToolBar(toolbar)

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
        self.timeline.track_heights_changed.connect(self.on_track_heights_changed)
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

        # Bundle the timeline toolbar + tracks into one resizable section.
        timeline_section = QWidget()
        section_layout = QVBoxLayout(timeline_section)
        section_layout.setContentsMargins(0, 0, 0, 0)
        section_layout.setSpacing(0)
        section_layout.addWidget(self.timeline_toolbar)
        section_layout.addWidget(self.timeline)
        timeline_section.setMinimumHeight(160)

        # Vertical splitter: drag the divider to resize the timeline section.
        self.vertical_splitter = QSplitter(Qt.Orientation.Vertical)
        self.vertical_splitter.addWidget(self.main_content)
        self.vertical_splitter.addWidget(timeline_section)
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
        self.edit_stage_button = QPushButton("✎  Edit stage")
        self.edit_stage_button.setObjectName("EditStageButton")
        self.edit_stage_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edit_stage_button.clicked.connect(self.enter_edit_mode)
        self.edit_stage_button.adjustSize()
        stage_view.add_overlay(self.edit_stage_button, anchor="top-right", reference="background")
        self.edit_stage_button.show()

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
        sizes = self.main_content.sizes()
        if len(sizes) < 2:
            return
        total = sizes[0] + sizes[1]
        self._last_dmx_width = sizes[1] if sizes[1] > 0 else 250
        self.main_content.setSizes([total, 0])
        self.right_sidebar.setVisible(False)
        self._dmx_panel_collapsed = True
        self._update_dmx_toggle_button()

    def _toggle_dmx_panel(self) -> None:
        """Slide the DMX side panel in or out with a short animation."""
        if (self._panel_animation is not None
                and self._panel_animation.state() == QAbstractAnimation.State.Running):
            return

        sizes = self.main_content.sizes()
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
            lambda val: self.main_content.setSizes([total - int(val), int(val)])
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
            self.dmx_toggle_button.setText("◀")  # ◀
            self.dmx_toggle_button.setToolTip("Show DMX panel")
        else:
            self.dmx_toggle_button.setText("▶")  # ▶
            self.dmx_toggle_button.setToolTip("Hide DMX panel")

    def on_waveform_offset_changed(self, offset_sec):
        """Handle waveform offset changes from timeline."""
        self.media_controller.set_audio_offset(offset_sec)
        self.config.update_current_song(offset=offset_sec)
        self.save_config()
    
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
        
        was_running = self.audio_controller.is_metronome_running()
        current_position = self.media_controller.get_current_position() if was_running else 0.0
        
        self.audio_controller.set_bpm(value)
        self.timeline.set_bpm(value)
        
        # Restart metronome with new BPM if it was running
        if was_running:
            self.audio_controller.stop_metronome()
            self.audio_controller.start_metronome(current_position)
    
    def _on_metronome_beat(self, beat_number):
        """Handle metronome beat events."""
        # Update any UI that needs to respond to beats
        pass
    
    def _on_audio_error(self, error_message, error_type):
        """Handle audio system errors."""
        self.logger.error(f"Audio error ({error_type}): {error_message}")
        ErrorDialogManager.show_audio_hardware_error(self, error_message, error_type)

    def on_bpm_detected_from_timeline(self, value):
        """Handle BPM detection from timeline analysis."""
        self.timeline_toolbar.set_bpm(value)
        
        was_running = self.audio_controller.is_metronome_running()
        current_position = self.media_controller.get_current_position() if was_running else 0.0
        
        self.audio_controller.set_bpm(value)
        self.timeline.set_bpm(value)
        
        # Restart metronome with new BPM if it was running
        if was_running:
            self.audio_controller.stop_metronome()
            self.audio_controller.start_metronome(current_position)
    
    def _on_playback_state_changed(self, is_playing):
        """Handle playback state changes from media controller."""
        self.timeline_toolbar.update_play_pause_icon(is_playing)
        
        if is_playing:
            # Start metronome synchronized to current timeline position
            current_position = self.media_controller.get_current_position()
            self.audio_controller.start_metronome(current_position)
        else:
            self.audio_controller.stop_metronome()
    
    def _on_position_changed(self, position_seconds):
        """Handle position changes from media controller."""
        self.timeline.update_playhead(position_seconds)
        # Update toolbar time display
        minutes = int(position_seconds // 60)
        seconds = position_seconds % 60
        time_str = f"{minutes:02d}:{seconds:06.3f}"
        self.timeline_toolbar.update_time_display(time_str)
    
    def _on_media_error(self, error_message, file_path=None):
        """Handle media errors from media controller."""
        self.logger.error(f"Media error: {error_message}")
        
        # Show user-friendly error dialog
        if file_path:
            ErrorDialogManager.show_media_file_error(self, file_path, error_message)
        else:
            # Generic media error
            ErrorDialogManager.show_media_file_error(self, "Unknown file", error_message)

    def _apply_io_config(self):
        """Apply config.io end to end: MIDI routing + controller/sinks/stage."""
        self.media_controller.set_midi_routing(midi_routing_from_io(self.config.io))
        self.dmx_manager.apply_io(self.config.io)

    def on_dmx_status_changed(self, source, status):
        """Handle DMX connection status changes."""
        self.logger.info(f"DMX {source} status: {status}")
    
    def on_dmx_error(self, source, error):
        """Handle DMX errors."""
        self.logger.error(f"DMX {source} error: {error}")
        
        # Show user-friendly error dialog with retry option
        if ErrorDialogManager.show_dmx_connection_error(self, source, error):
            # User wants to retry: re-apply the stage-input leg from config.io
            self.dmx_manager.restart_stage_input()
    
    def on_midi_status_changed(self, port_name, status):
        """Handle MIDI connection status changes."""
        self.logger.info(f"MIDI {port_name} status: {status}")
    
    def on_midi_error(self, port_name, error):
        """Handle MIDI errors."""
        self.logger.error(f"MIDI {port_name} error: {error}")
        
        # Show user-friendly error dialog with retry option
        if ErrorDialogManager.show_midi_connection_error(self, port_name, error):
            # User wants to retry: re-apply the routing (restarts a live worker)
            self.media_controller.set_midi_routing(midi_routing_from_io(self.config.io))
    
    def on_midi_finished(self):
        """Handle MIDI worker finished signal."""
        self.logger.info("MIDI playback finished")

    def on_software_controller_changed(self, controller):
        """Handle software controller instance changes."""
        self.logger.info(f"Software controller changed: {controller}")
        self.software_controller = controller
        self.midi_manager.set_software_controller(controller)
        # Live channel editing in the DMX monitor only works when a software
        # controller is present to receive the overrides (its universe size caps
        # which channels are editable).
        self.dmx_display.set_editing_enabled(
            controller is not None,
            len(controller.present) if controller is not None else 0)
        # Seed the fresh controller from the document's persisted per-track setup
        # (note 71), then re-sync the 'A' buttons to the restored state.
        self.midi_manager.seed_controller_auto(controller)
        self.timeline.refresh_auto_buttons()
        # Redraw so the auto-mode song-part visualization appears when a
        # software controller attaches and clears when it detaches.
        self.timeline._schedule_refresh()

    def _preview_clip(self, messages):
        """Audition clip-library items live: route note_on(s) through the
        configured MIDI outputs without modifying the timeline. `messages` is
        a list of (channel, note, velocity) tuples — length 1 for clips, 7 for
        LED scenes. The channel selects the target fixture (its patch index)."""
        if not messages:
            return
        routing = midi_routing_from_io(self.config.io)
        if not routing.has_output():
            self.logger.debug("Preview ignored: no MIDI outputs configured")
            return

        if routing.to_controller:
            controller = getattr(self, "software_controller", None)
            if controller is None:
                self.logger.debug(
                    "Preview: software controller routed but not active")
            else:
                for channel, note, velocity in messages:
                    try:
                        controller.handle_note_on(
                            int(channel), int(note), int(velocity))
                    except Exception as e:
                        self.logger.warning(
                            f"Preview to software controller failed: {e}")

        if not routing.port:
            return

        # External port: reuse the running worker's open handle when it
        # matches, since some MIDI backends refuse a second open on the
        # same device. Otherwise open a transient port for this preview.
        worker = self.midi_manager.current_worker
        if (worker and worker.isRunning() and worker.port
                and worker.port_name == routing.port):
            for channel, note, velocity in messages:
                try:
                    worker.port.send(mido.Message(
                        'note_on', channel=int(channel),
                        note=int(note), velocity=int(velocity)))
                except Exception as e:
                    self.logger.warning(
                        f"Preview via worker port failed: {e}")
            return

        try:
            with mido.open_output(routing.port) as port:
                for channel, note, velocity in messages:
                    port.send(mido.Message(
                        'note_on', channel=int(channel),
                        note=int(note), velocity=int(velocity)))
        except Exception as e:
            self.logger.warning(
                f"Preview open/send on '{routing.port}' failed: {e}")

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

        dialog = SettingsDialog(self.config, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        # Re-apply the routing when it (or the Art-Net target the sinks and
        # listener read from) changed.
        if asdict(self.config.io) != old_io or asdict(self.config.artnet) != old_artnet:
            self._apply_io_config()

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
        Excludes x/y so a stage drag-move doesn't churn the UI."""
        return tuple((f.type, f.dmx_start, f.channel, f.name)
                     for f in self.config.fixtures)

    def _patch_dmx_signature(self):
        """What the software DMX engine depends on (per fixture). Excludes the
        name so a rename never re-patches (and never blacks out) the rig."""
        return tuple((f.type, f.dmx_start, f.channel)
                     for f in self.config.fixtures)

    def _on_patch_changed(self):
        """Persist a fixture-patch edit and propagate it live: rebuild the
        timeline tracks, refresh the clip library + DMX monitor, and hot-swap the
        running software DMX engine — each gated by a cheap signature so stage
        drag-moves (and, for the engine, renames) don't cause needless churn."""
        self.config.mark_patch_dirty()
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
            controller.repatch(self.config.fixtures)

    def on_audio_volume_changed(self, value):
        """Handle audio volume changes from timeline."""
        volume = float(value) / 100.0
        self.media_controller.set_audio_volume(volume)
        self.config.update_volume(audio=volume)
        self.save_config()

    def on_audio_mute_toggled(self, muted):
        """Handle audio mute toggle from timeline."""
        self.media_controller.set_audio_muted(muted)

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
                    
                elif file_type == 'midi':
                    # Update current song's MIDI path
                    self.config.update_current_song(midi=path)
                    self.save_config()
                    
                    # Load MIDI file via MidiManager and update display
                    if self.midi_manager.load_from_file(path):
                        self.timeline.refresh_from_manager(detect_bpm=True)
                        self.toolbar_manager.update_midi_file_display(path)
                        self.toolbar_manager.set_export_midi_enabled(True)
                    else:
                        ErrorDialogManager.show_media_file_error(self, path, "Failed to load MIDI file")
                        return
                
                self.update_window_title()
                    
            except PermissionError:
                ErrorDialogManager.show_file_permission_error(self, path, "read")
            except Exception as e:
                ErrorDialogManager.show_media_file_error(self, path, str(e))

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
                
                # Export via MidiManager
                if self.midi_manager.export_to_file(path, self.timeline_toolbar.get_bpm()):
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
        """Save configuration with error handling."""
        prefs_file = str(userprefs_path())
        try:
            if not self.config.save():
                ErrorDialogManager.show_file_permission_error(self, prefs_file, 'save')
        except Exception as e:
            self.logger.error(f"Failed to save configuration: {e}")
            ErrorDialogManager.show_file_permission_error(self, prefs_file, 'save')

    def start_playback(self):
        """Start media playback using media controller."""
        self.media_controller.start_playback()

    def play_pause_playback(self):
        """Toggle playback using media controller."""
        self.media_controller.play_pause_playback()

    def stop_playback(self):
        """Stop playback using media controller."""
        self.media_controller.stop_playback()
        self.timeline.update_playhead(0)

    def seek_playback(self, time_seconds):
        """Seek to position using media controller."""
        was_playing = self.media_controller.get_playback_state()
        metronome_was_running = self.audio_controller.is_metronome_running()
        
        self.media_controller.seek_to_position(time_seconds)
        
        # Resynchronize metronome if it was running
        if was_playing and metronome_was_running:
            self.audio_controller.stop_metronome()
            self.audio_controller.start_metronome(time_seconds)

    def update_window_title(self):
        """Update window title with loaded file names."""
        title = "TupperDMX Simulator"
        current_song = self.config.get_current_song()
        if current_song and current_song.audio:
            file_name = os.path.basename(current_song.audio)
            title += f" - {file_name}"
        self.setWindowTitle(title)

    def eventFilter(self, source, event):
        """Global event filter to capture spacebar from any widget."""
        if (event.type() == QEvent.Type.KeyPress and 
            event.key() == Qt.Key.Key_Space):
            # Toggle play/pause when spacebar is pressed anywhere
            self.play_pause_playback()
            return True  # Event handled, don't pass to widget
        return super().eventFilter(source, event)

    def closeEvent(self, event):
        """Handle application close event."""
        self.logger.info("Application closing - cleaning up resources")
        
        # Remove global event filter
        QApplication.instance().removeEventFilter(self)
        
        # Stop playback and cleanup components — guard each so one failure
        # doesn't abort the rest of the shutdown.
        for name, cleanup in (
            ("media_controller", self.media_controller.cleanup),
            ("audio_controller", self.audio_controller.cleanup),
            ("dmx_manager", self.dmx_manager.cleanup),
            ("midi_manager", self.midi_manager.cleanup),
        ):
            try:
                cleanup()
            except Exception as e:
                self.logger.error(f"Error cleaning up {name}: {e}")

        # Save configuration
        self.save_config()

        # Accept the close event
        event.accept()
