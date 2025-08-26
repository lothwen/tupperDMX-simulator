import sys
import os
import json
import time
import numpy as np

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QComboBox, QFileDialog, QGraphicsView, QGraphicsScene,
                             QTextEdit, QSplitter, QLabel, QTableWidget, QTableWidgetItem, QSpinBox, QToolBar, QSizePolicy, QStyle)
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput, QAudioSink, QAudioFormat
from PyQt6.QtGui import QPixmap, QBrush, QFont, QColor, QAction
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QUrl, QTimer

from config import Config
from fixtures import LedBar, Blinder
from timeline import Timeline
from dmx import DMXManager
from midi import MidiManager
from tupperlogger import setup_logging, get_logger
from ui import MediaController, DMXDisplayWidget, AudioController, StageController, ToolbarManager, ErrorDialogManager

# StageView moved to ui.stage_controller module


class MainWindow(QMainWindow):
    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.logger = get_logger("ui.main")
        
        # Initialize managers
        self.dmx_manager = DMXManager(self.config)
        self.dmx_manager.dmx_data_received.connect(self.update_dmx)
        self.dmx_manager.connection_status_changed.connect(self.on_dmx_status_changed)
        self.dmx_manager.error_occurred.connect(self.on_dmx_error)
        
        self.midi_manager = MidiManager(self.config.midi_note_map)
        self.midi_manager.connection_status_changed.connect(self.on_midi_status_changed)
        self.midi_manager.error_occurred.connect(self.on_midi_error)
        self.midi_manager.data_changed.connect(self.on_midi_data_changed_from_manager)
        
        # Initialize UI components
        self._setup_ui_components()
        
        # Window setup
        self.update_window_title()
        self.setGeometry(100, 100, self.config.window.width, self.config.window.height)
         
        # Setup main layout
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QVBoxLayout(self.central_widget)
        self.main_layout.setContentsMargins(0, 0, 0, 0)  # Remove all margins
        self.main_layout.setSpacing(0)  # Remove spacing between widgets
        
        # Create UI
        self.setup_ui()
        
        # Set initial files
        if self.config.files.audio:
            self.media_controller.set_files(self.config.files.audio)
            self.timeline.set_files(self.config.files.audio)
            # Update audio file display
            self.toolbar_manager.update_audio_file_display(self.config.files.audio)
        
        # Load MIDI file via MidiManager
        if self.config.files.midi:
            if self.midi_manager.load_from_file(self.config.files.midi):
                self.timeline.refresh_from_manager()
                self.toolbar_manager.set_export_midi_enabled(True)
            else:
                self.logger.error(f"Failed to load MIDI file: {self.config.files.midi}")
        else:
            # Create empty MIDI track if no file configured
            self.midi_manager.create_empty_track()
            self.timeline.refresh_from_manager()
        
        # Set initial MIDI port
        initial_midi_port = self.toolbar_manager.get_selected_midi_port()
        if initial_midi_port:
            self.media_controller.set_midi_port(initial_midi_port)
            self.logger.info(f"Initial MIDI port set to: {initial_midi_port}")
        
        # Start DMX with last used source
        initial_dmx_source = self.config.last_dmx_source or "Art-Net"
        if initial_dmx_source in self.dmx_manager.get_available_sources():
            self.toolbar_manager.set_dmx_source(initial_dmx_source)
            self.dmx_manager.start_worker(initial_dmx_source)
    
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
        
        self.audio_controller = AudioController(self.config.metronome, self.config.volume.metronome)
        self.audio_controller.beat_occurred.connect(self._on_metronome_beat)
        self.audio_controller.audio_error_occurred.connect(self._on_audio_error)
        
        self.stage_controller = StageController(self.config)
        
        self.toolbar_manager = ToolbarManager(self.config, self.dmx_manager, self.midi_manager, 
                                            play_icon, pause_icon, stop_icon)
        self._connect_toolbar_signals()

    def _connect_toolbar_signals(self):
        """Connect toolbar manager signals to appropriate handlers."""
        self.toolbar_manager.load_file_requested.connect(self.open_file_dialog)
        self.toolbar_manager.export_midi_requested.connect(self.export_midi_dialog)
        self.toolbar_manager.play_pause_requested.connect(self.play_pause_playback)
        self.toolbar_manager.stop_requested.connect(self.stop_playback)
        self.toolbar_manager.bpm_changed.connect(self.bpm_changed)
        self.toolbar_manager.dmx_source_changed.connect(self.on_dmx_source_changed)
        self.toolbar_manager.midi_port_changed.connect(self.set_midi_port)
        self.toolbar_manager.metronome_mute_toggled.connect(self.on_toolbar_metronome_mute_toggled)
    
    def setup_ui(self):
        """Setup the main UI layout."""
        # Create and add toolbar
        toolbar = self.toolbar_manager.create_toolbar(self)
        self.addToolBar(toolbar)

        # Create main content splitter
        main_content = QSplitter(Qt.Orientation.Horizontal)
        self.main_layout.addWidget(main_content, 1)
        
        # Add stage view
        stage_view = self.stage_controller.get_stage_view()
        main_content.addWidget(stage_view)
        
        # Add DMX display
        main_content.addWidget(self.dmx_display)
        
        # Create timeline with MidiManager reference
        self.timeline = Timeline(
            midi_manager=self.midi_manager,
            initial_metronome_volume=self.config.volume.metronome,
            initial_audio_volume=self.config.volume.audio
        )
        self.timeline.setFixedHeight(300)
        self.timeline.seek_requested.connect(self.seek_playback)
        self.timeline.bpm_detected.connect(self.on_bpm_detected_from_timeline)
        self.timeline.waveform_offset_changed.connect(self.on_waveform_offset_changed)
        self.timeline.metronome_volume_changed.connect(self.on_metronome_volume_changed)
        self.timeline.metronome_mute_toggled.connect(self.on_metronome_mute_toggled)
        self.timeline.audio_volume_changed.connect(self.on_audio_volume_changed)
        self.timeline.audio_mute_toggled.connect(self.on_audio_mute_toggled)
        # Timeline no longer emits midi_data_changed - handled by MidiManager
        self.main_layout.addWidget(self.timeline)
        
        main_content.setSizes([self.width() - 250, 250])

        # Setup DMX display (using widget created in _setup_ui_components)
        # DMX display is already configured, just trigger initial update
        self.update_dmx_display()

    def on_waveform_offset_changed(self, offset_sec):
        """Handle waveform offset changes from timeline."""
        self.media_controller.set_audio_offset(offset_sec)
    
    def bpm_changed(self, value):
        """Handle BPM changes from toolbar."""
        # Validate BPM range
        if not (40 <= value <= 300):
            ErrorDialogManager.show_validation_error(
                self, "BPM", str(value), "a value between 40 and 300"
            )
            # Reset to valid range
            value = max(40, min(300, value))
            self.toolbar_manager.set_bpm(value)
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
        self.toolbar_manager.set_bpm(value)
        
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
        self.toolbar_manager.update_play_pause_icon(is_playing)
        
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
        self.toolbar_manager.update_time_display(time_str)
    
    def _on_media_error(self, error_message, file_path=None):
        """Handle media errors from media controller."""
        self.logger.error(f"Media error: {error_message}")
        
        # Show user-friendly error dialog
        if file_path:
            ErrorDialogManager.show_media_file_error(self, file_path, error_message)
        else:
            # Generic media error
            ErrorDialogManager.show_media_file_error(self, "Unknown file", error_message)

    def on_dmx_source_changed(self, source):
        """Handle DMX source change from UI."""
        self.dmx_manager.switch_source(source)
        self.save_config()
    
    def on_dmx_source_changed_internal(self, source):
        """Handle DMX source change from manager."""
        self.config.last_dmx_source = source
    
    def on_dmx_status_changed(self, source, status):
        """Handle DMX connection status changes."""
        self.logger.info(f"DMX {source} status: {status}")
    
    def on_dmx_error(self, source, error):
        """Handle DMX errors."""
        self.logger.error(f"DMX {source} error: {error}")
        
        # Show user-friendly error dialog with retry option
        if ErrorDialogManager.show_dmx_connection_error(self, source, error):
            # User wants to retry
            self.dmx_manager.switch_source(source)
    
    def on_midi_status_changed(self, port_name, status):
        """Handle MIDI connection status changes."""
        self.logger.info(f"MIDI {port_name} status: {status}")
    
    def on_midi_error(self, port_name, error):
        """Handle MIDI errors."""
        self.logger.error(f"MIDI {port_name} error: {error}")
        
        # Show user-friendly error dialog with retry option
        if ErrorDialogManager.show_midi_connection_error(self, port_name, error):
            # User wants to retry
            self.media_controller.set_midi_port(port_name)
    
    def on_midi_finished(self):
        """Handle MIDI worker finished signal."""
        self.logger.info("MIDI playback finished")


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
        """Handle metronome mute toggle from timeline."""
        self.audio_controller.set_metronome_muted(muted)
        # Update toolbar button to reflect mute state
        self.toolbar_manager.set_metronome_mute(muted)

    def on_toolbar_metronome_mute_toggled(self, muted):
        """Handle metronome mute toggle from toolbar."""
        self.audio_controller.set_metronome_muted(muted)
        # Update timeline 'M' button to reflect mute state
        self.timeline.metronome_mute_button.setChecked(muted)

    def on_audio_volume_changed(self, value):
        """Handle audio volume changes from timeline."""
        volume = float(value) / 100.0
        self.media_controller.set_audio_volume(volume)
        self.config.update_volume(audio=volume)
        self.save_config()

    def on_audio_mute_toggled(self, muted):
        """Handle audio mute toggle from timeline."""
        self.media_controller.set_audio_muted(muted)

    def on_midi_data_changed(self):
        """Deprecated - MIDI data changes are now handled by MidiManager."""
        pass
        
    def on_midi_data_changed_from_manager(self):
        """Handle when MIDI data is modified via MidiManager."""
        self.logger.info("MIDI data has been modified via MidiManager")
        
        # Refresh the timeline display
        if self.timeline:
            self.timeline.refresh_from_manager()
        
        # Enable export button if we have MIDI data
        has_midi_data = self.midi_manager.midi_data is not None
        self.toolbar_manager.set_export_midi_enabled(has_midi_data)

    def update_dmx_display(self):
        """Update DMX display - now handled by DMXDisplayWidget component."""
        # DMX display updates are now handled by the DMXDisplayWidget component
        # through the update_dmx() method which calls dmx_display.update_dmx_data()
        pass
            


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
                
                # Update configuration
                if file_type == 'midi':
                    self.config.update_files(midi=path)
                elif file_type == 'audio':  # This handles all audio files (mp3, wav)
                    # Validate file extension
                    file_ext = path.lower().split('.')[-1]
                    if file_ext not in ['mp3', 'wav']:
                        ErrorDialogManager.show_media_file_error(self, path, "Unsupported audio format. Please select an MP3 or WAV file.")
                        return
                    self.config.update_files(audio=path)
                self.save_config()
                
                # Update components with new files
                self.media_controller.set_files(self.config.files.audio, self.config.files.midi)
                self.timeline.set_files(self.config.files.audio, self.config.files.midi)
                
                # Update toolbar audio file display
                if file_type == 'audio':
                    # Show full path instead of just filename
                    self.toolbar_manager.update_audio_file_display(path)
                
                # Enable/disable export button based on MIDI file availability
                self.toolbar_manager.set_export_midi_enabled(bool(self.config.files.midi))
                
                self.update_window_title()
                    
            except PermissionError:
                ErrorDialogManager.show_file_permission_error(self, path, "read")
            except Exception as e:
                ErrorDialogManager.show_media_file_error(self, path, str(e))

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
        if self.config.files.midi:
            base_name = os.path.splitext(os.path.basename(self.config.files.midi))[0]
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
                if self.midi_manager.export_to_file(path):
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

    def set_midi_port(self, port_name):
        """Handle MIDI port selection from toolbar."""
        self.config.last_midi_port = port_name
        self.save_config()
        
        # Update media controller with new port
        self.media_controller.set_midi_port(port_name)

    def save_config(self):
        """Save configuration with error handling."""
        try:
            if not self.config.save('config.json'):
                ErrorDialogManager.show_file_permission_error(self, 'config.json', 'save')
        except Exception as e:
            self.logger.error(f"Failed to save configuration: {e}")
            ErrorDialogManager.show_file_permission_error(self, 'config.json', 'save')

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
        if self.config.files.audio:
            file_name = os.path.basename(self.config.files.audio)
            title += f" - {file_name}"
        self.setWindowTitle(title)

    def closeEvent(self, event):
        """Handle application close event."""
        self.logger.info("Application closing - cleaning up resources")
        
        # Stop playback and cleanup components
        self.media_controller.cleanup()
        self.audio_controller.cleanup()
        self.dmx_manager.cleanup()
        self.midi_manager.cleanup()
        
        # Save configuration
        self.save_config()
        
        # Accept the close event
        event.accept()
        self.midi_manager.cleanup()
        event.accept()

def main():
    app = QApplication(sys.argv)
    
    # Load and apply dark theme
    qss_path = os.path.join(os.path.dirname(__file__), 'dark-theme.qss')
    if os.path.exists(qss_path):
        with open(qss_path, 'r', encoding='utf-8') as f:
            app.setStyleSheet(f.read())
    
    try:
        config = Config.load('config.json')
    except Exception as e:
        # Show configuration error before main window is created
        from ui.error_dialogs import ErrorDialogManager
        if ErrorDialogManager.show_configuration_error(None, str(e), can_reset=True):
            # User chose to reset configuration
            try:
                os.remove('config.json')
                config = Config.load('config.json')  # Will create new default config
            except Exception as reset_error:
                # If we can't even reset, use default config in memory
                config = Config()
        else:
            # Use default configuration
            config = Config()
    
    # Initialize logging system
    try:
        setup_logging(
            level=config.logging.level,
            console_output=config.logging.console_output,
            file_output=config.logging.file_output,
            log_dir=config.logging.log_dir,
            max_file_size=config.logging.max_file_size,
            backup_count=config.logging.backup_count
        )
    except Exception as e:
        # Fallback to basic logging if setup fails
        import logging
        logging.basicConfig(level=logging.INFO)
        logger = logging.getLogger("system.startup")
        logger.error(f"Failed to setup logging system: {e}")
    
    logger = get_logger("system.startup")
    logger.info("Starting TupperDMX Simulator")
    logger.info(f"Configuration loaded from config.json")
    logger.debug(f"Logging level: {config.logging.level}")

    try:
        main_window = MainWindow(config)
        main_window.show()
        
        logger.info("Application started successfully")
        
        sys.exit(app.exec())
    except KeyboardInterrupt:
        logger.info("Application interrupted by user")
    except Exception as e:
        logger.critical(f"Application crashed: {e}")
        
        # Show crash dialog if possible
        try:
            from ui.error_dialogs import ErrorDialog
            crash_dialog = ErrorDialog(
                None, 
                "Application Crash", 
                "The application encountered an unexpected error and needs to close.",
                str(e), 
                "error"
            )
            crash_dialog.exec()
        except:
            pass  # If we can't show dialog, just exit
        
        raise

if __name__ == "__main__":
    main()
