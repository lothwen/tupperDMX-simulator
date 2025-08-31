from typing import Callable, Optional
from PyQt6.QtWidgets import QToolBar, QSpinBox, QComboBox, QLabel, QWidget, QSizePolicy, QFrame, QHBoxLayout, QPushButton, QVBoxLayout
from PyQt6.QtGui import QAction, QIcon, QFont
from PyQt6.QtCore import QObject, pyqtSignal, Qt, QSize

from tupperlogger import get_logger
from dmx import DMXManager
from midi import MidiManager
from config import Config


class ToolbarManager(QObject):
    """Manages the main application toolbar with media controls and configuration options."""
    
    # Signals
    load_file_requested = pyqtSignal(str)  # file_type ('midi' or 'audio')
    new_midi_track_requested = pyqtSignal()  # request to create new empty MIDI track
    export_midi_requested = pyqtSignal()  # request to export MIDI file
    play_pause_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    bpm_changed = pyqtSignal(int)  # new_bpm
    dmx_source_changed = pyqtSignal(str)  # source_name
    midi_port_changed = pyqtSignal(str)  # port_name
    metronome_mute_toggled = pyqtSignal(bool)  # mute_state
    
    def __init__(self, config: Config, dmx_manager: DMXManager, midi_manager: MidiManager, 
                 play_icon: QIcon, pause_icon: QIcon, stop_icon: QIcon):
        super().__init__()
        self.config = config
        self.dmx_manager = dmx_manager
        self.midi_manager = midi_manager
        self.logger = get_logger("ui.toolbar")
        
        # Icons
        self.play_icon = play_icon
        self.pause_icon = pause_icon
        self.stop_icon = stop_icon
        
        # Toolbar and widgets
        self.toolbar: Optional[QToolBar] = None
        self.play_pause_button: Optional[QPushButton] = None
        self.export_midi_button: Optional[QPushButton] = None
        self.bpm_spinbox: Optional[QSpinBox] = None
        self.metronome_button: Optional[QPushButton] = None
        self.dmx_source_combo: Optional[QComboBox] = None
        self.midi_port_combo: Optional[QComboBox] = None
        self.time_display: Optional[QLabel] = None
        self.audio_file_label: Optional[QLabel] = None
        self.midi_file_label: Optional[QLabel] = None
        
        # State
        self.is_playing = False
        
        self.logger.info("Toolbar manager initialized")
    
    def create_toolbar(self, parent) -> QToolBar:
        """Create and setup the main toolbar with grouped sections."""
        self.toolbar = QToolBar("Main Toolbar", parent)
        self.toolbar.setFixedHeight(120)
        self.toolbar.setMovable(False)
        self.toolbar.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        
        # Create a container widget with horizontal layout to properly manage expansion
        container = QWidget()
        container_layout = QHBoxLayout(container)
        container_layout.setContentsMargins(10, 10, 10, 10)
        container_layout.setSpacing(15)
        
        # Files Section (left - expandable to fill left space)
        files_section = self._create_files_section()
        container_layout.addWidget(files_section, 1)  # stretch factor 1 = expandable
        
        # Center group container for Transport + Timeline
        center_container = QWidget()
        center_layout = QHBoxLayout(center_container)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(15)
        
        # Transport Controls Section (center - fixed)
        transport_section = self._create_transport_section()
        center_layout.addWidget(transport_section)
        
        # Add center container as fixed (no stretch)
        container_layout.addWidget(center_container, 0)  # stretch factor 0 = fixed/centered
        
        # Sources Section (right - expandable to fill right space)
        sources_section = self._create_sources_section()
        container_layout.addWidget(sources_section, 1)  # stretch factor 1 = expandable
        
        # Add the container to the toolbar
        self.toolbar.addWidget(container)
        
        self.logger.info("Main toolbar created")
        return self.toolbar
    
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
        
        # Load MIDI button
        load_midi_btn = QPushButton("Load MIDI file")
        load_midi_btn.setFixedSize(150, 30)
        load_midi_btn.setObjectName("LoadButton")
        load_midi_btn.clicked.connect(lambda: self.load_file_requested.emit('midi'))
        buttons_layout.addWidget(load_midi_btn)
        
        # Load Audio button
        load_audio_btn = QPushButton("Load Audio file")
        load_audio_btn.setFixedSize(150, 30)
        load_audio_btn.setObjectName("LoadButton")
        load_audio_btn.clicked.connect(lambda: self.load_file_requested.emit('audio'))
        buttons_layout.addWidget(load_audio_btn)
        
        # New MIDI track button
        new_midi_btn = QPushButton("New MIDI track")
        new_midi_btn.setFixedSize(150, 30)
        new_midi_btn.setObjectName("NewButton")
        new_midi_btn.clicked.connect(self.new_midi_track_requested.emit)
        buttons_layout.addWidget(new_midi_btn)

        # Export MIDI button
        self.export_midi_button = QPushButton("Export MIDI")
        self.export_midi_button.setFixedSize(150, 30)
        self.export_midi_button.setObjectName("ExportButton")
        self.export_midi_button.clicked.connect(self.export_midi_requested.emit)
        self.export_midi_button.setEnabled(False)  # Disabled by default
        buttons_layout.addWidget(self.export_midi_button)
        
        layout.addLayout(buttons_layout)
        
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

    def _create_transport_section(self) -> QFrame:
        """Create the transport controls section."""
        section = QFrame()
        section.setFrameStyle(QFrame.Shape.StyledPanel)
        section.setObjectName("ToolbarSection")
        
        layout = QVBoxLayout(section)
        layout.setContentsMargins(10, 5, 10, 5)
        
        # Section title
        title = QLabel("TRANSPORT")
        title.setObjectName("SectionTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        
        # Controls layout
        controls_layout = QHBoxLayout()
        controls_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        # Stop button
        stop_btn = QPushButton()
        stop_btn.setIcon(self.stop_icon)
        stop_btn.setFixedSize(40, 40)
        stop_btn.setObjectName("StopButton")
        stop_btn.clicked.connect(self.stop_requested.emit)
        controls_layout.addWidget(stop_btn)
        
        # Play/Pause button (larger)
        self.play_pause_button = QPushButton()
        self.play_pause_button.setIcon(self.play_icon)
        self.play_pause_button.setFixedSize(50, 50)
        self.play_pause_button.setObjectName("PlayPauseButton")
        self.play_pause_button.clicked.connect(self.play_pause_requested.emit)
        controls_layout.addWidget(self.play_pause_button)
        
        # Time display
        self.time_display = QLabel("00:00.000")
        self.time_display.setFixedSize(120, 50)
        self.time_display.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.time_display.setObjectName("TimeDisplay")
        controls_layout.addWidget(self.time_display)
        
        # BPM display with metronome icon and spinbox
        bpm_container = QWidget()
        bpm_layout = QHBoxLayout(bpm_container)
        bpm_layout.setContentsMargins(0, 0, 0, 0)
        bpm_layout.setSpacing(2)
        
        # Metronome icon button (clickable)
        self.metronome_button = QPushButton()
        metronome_icon = QIcon("assets/icons/metronome_icon.png")
        self.metronome_button.setIcon(metronome_icon)
        self.metronome_button.setIconSize(QSize(28, 28))  # Set icon size explicitly
        self.metronome_button.setFixedSize(32, 32)
        self.metronome_button.setCheckable(True)
        self.metronome_button.setObjectName("MetronomeButton")
        self.metronome_button.toggled.connect(self.metronome_mute_toggled.emit)
        bpm_layout.addWidget(self.metronome_button)
        
        self.bpm_spinbox = QSpinBox()
        self.bpm_spinbox.setRange(40, 300)
        self.bpm_spinbox.setValue(120)
        self.bpm_spinbox.setFixedSize(50, 30)
        self.bpm_spinbox.valueChanged.connect(self.bpm_changed.emit)
        bpm_layout.addWidget(self.bpm_spinbox)
        
        controls_layout.addWidget(bpm_container)

        layout.addLayout(controls_layout)
        
        self.logger.debug("Transport section created")
        return section

    def _create_sources_section(self) -> QFrame:
        """Create the sources section."""
        section = QFrame()
        section.setFrameStyle(QFrame.Shape.StyledPanel)
        section.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        section.setObjectName("ToolbarSection")
        
        layout = QVBoxLayout(section)
        layout.setContentsMargins(10, 5, 10, 5)
        
        # Section title
        title = QLabel("SOURCES")
        title.setObjectName("SectionTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
       
        # Sources layout with more spacing
        sources_layout = QHBoxLayout()
        sources_layout.setSpacing(20)  # More space between MIDI and DMX
        
        # MIDI source column
        midi_container = QWidget()
        midi_layout = QVBoxLayout(midi_container)
        midi_layout.setContentsMargins(0, 0, 0, 0)
        midi_layout.setSpacing(5)
        
        midi_label = QLabel("MIDI Port")
        midi_label.setObjectName("SubsectionLabel")
        midi_layout.addWidget(midi_label)
        
        self.midi_port_combo = QComboBox()
        self.midi_port_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.midi_port_combo.setMinimumHeight(25)
        available_ports = self.midi_manager.get_available_ports()
        self.midi_port_combo.addItems(available_ports)
        if self.config.last_midi_port and self.config.last_midi_port in available_ports:
            self.midi_port_combo.setCurrentText(self.config.last_midi_port)
        self.midi_port_combo.currentTextChanged.connect(self.midi_port_changed.emit)
        midi_layout.addWidget(self.midi_port_combo)
        
        sources_layout.addWidget(midi_container, 1)  # expandable
        
        # DMX source column
        dmx_container = QWidget()
        dmx_layout = QVBoxLayout(dmx_container)
        dmx_layout.setContentsMargins(0, 0, 0, 0)
        dmx_layout.setSpacing(5)
        
        dmx_label = QLabel("DMX Input")
        dmx_label.setObjectName("SubsectionLabel")
        dmx_layout.addWidget(dmx_label)
        
        self.dmx_source_combo = QComboBox()
        self.dmx_source_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.dmx_source_combo.setMinimumHeight(25)
        dmx_sources = self.dmx_manager.get_available_sources()
        self.dmx_source_combo.addItems(dmx_sources)
        self.dmx_source_combo.currentTextChanged.connect(self.dmx_source_changed.emit)
        dmx_layout.addWidget(self.dmx_source_combo)
        
        sources_layout.addWidget(dmx_container, 1)  # expandable
        
        layout.addLayout(sources_layout)
        
        self.logger.debug("Sources section created")
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

    def update_time_display(self, time_str: str) -> None:
        """Update the time display."""
        if hasattr(self, 'time_display') and self.time_display:
            self.time_display.setText(time_str)

    def update_play_pause_icon(self, is_playing: bool) -> None:
        """Update the play/pause button icon based on current state."""
        self.is_playing = is_playing
        if self.play_pause_button:
            icon = self.pause_icon if is_playing else self.play_icon
            self.play_pause_button.setIcon(icon)
            self.logger.debug(f"Play/pause icon updated: {'pause' if is_playing else 'play'}")
    
    def set_bpm(self, bpm: int) -> None:
        """Set the BPM value in the spinbox."""
        if self.bpm_spinbox:
            self.bpm_spinbox.setValue(bpm)
            self.logger.debug(f"BPM spinbox set to {bpm}")
    
    def get_bpm(self) -> int:
        """Get current BPM value from spinbox."""
        if self.bpm_spinbox:
            return self.bpm_spinbox.value()
        return 120
    
    def get_selected_dmx_source(self) -> str:
        """Get currently selected DMX source."""
        if self.dmx_source_combo:
            return self.dmx_source_combo.currentText()
        return ""
    
    def get_selected_midi_port(self) -> str:
        """Get currently selected MIDI port."""
        if self.midi_port_combo:
            return self.midi_port_combo.currentText()
        return ""
    
    def refresh_dmx_sources(self) -> None:
        """Refresh the list of available DMX sources."""
        if self.dmx_source_combo:
            current_selection = self.dmx_source_combo.currentText()
            
            self.dmx_source_combo.clear()
            dmx_sources = self.dmx_manager.get_available_sources()
            self.dmx_source_combo.addItems(dmx_sources)
            
            # Restore selection if still available
            if current_selection in dmx_sources:
                self.dmx_source_combo.setCurrentText(current_selection)
            
            self.logger.debug(f"DMX sources refreshed: {len(dmx_sources)} sources")
    
    def refresh_midi_ports(self) -> None:
        """Refresh the list of available MIDI ports."""
        if self.midi_port_combo:
            current_selection = self.midi_port_combo.currentText()
            
            self.midi_port_combo.clear()
            available_ports = self.midi_manager.get_available_ports()
            self.midi_port_combo.addItems(available_ports)
            
            # Restore selection if still available
            if current_selection in available_ports:
                self.midi_port_combo.setCurrentText(current_selection)
            elif self.config.last_midi_port and self.config.last_midi_port in available_ports:
                self.midi_port_combo.setCurrentText(self.config.last_midi_port)
            
            self.logger.debug(f"MIDI ports refreshed: {len(available_ports)} ports")
    
    def set_dmx_source(self, source_name: str) -> None:
        """Set the selected DMX source."""
        if self.dmx_source_combo:
            index = self.dmx_source_combo.findText(source_name)
            if index >= 0:
                self.dmx_source_combo.setCurrentIndex(index)
                self.logger.debug(f"DMX source set to: {source_name}")
            else:
                self.logger.warning(f"DMX source not found: {source_name}")
    
    def set_midi_port(self, port_name: str) -> None:
        """Set the selected MIDI port."""
        if self.midi_port_combo:
            index = self.midi_port_combo.findText(port_name)
            if index >= 0:
                self.midi_port_combo.setCurrentIndex(index)
                self.logger.debug(f"MIDI port set to: {port_name}")
            else:
                self.logger.warning(f"MIDI port not found: {port_name}")
    
    def set_export_midi_enabled(self, enabled: bool) -> None:
        """Enable or disable the export MIDI button."""
        if self.export_midi_button:
            self.export_midi_button.setEnabled(enabled)
            self.logger.debug(f"Export MIDI button {'enabled' if enabled else 'disabled'}")
    
    def set_metronome_mute(self, is_muted: bool) -> None:
        """Set the metronome mute button state."""
        if self.metronome_button:
            self.metronome_button.setChecked(is_muted)
            self.logger.debug(f"Metronome mute button set to: {'muted' if is_muted else 'unmuted'}")

    def update_configuration(self, config: Config) -> None:
        """Update toolbar with new configuration."""
        self.config = config
        self.refresh_dmx_sources()
        self.refresh_midi_ports()
        self.logger.info("Toolbar configuration updated")