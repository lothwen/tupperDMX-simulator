import os
import time
from typing import Optional
from PyQt6.QtCore import QObject, pyqtSignal, QTimer, QUrl
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput

from tupperlogger import get_logger
from midi import MidiManager


class MediaController(QObject):
    """Handles media playback control (MP3 audio + MIDI synchronization)."""
    
    # Signals
    playback_state_changed = pyqtSignal(bool)  # is_playing
    position_changed = pyqtSignal(float)  # current time in seconds
    media_loaded = pyqtSignal()
    error_occurred = pyqtSignal(str, str)  # error message, file_path
    
    def __init__(self, midi_manager: MidiManager, audio_volume: float = 0.5):
        super().__init__()
        self.logger = get_logger("ui.media")
        self.midi_manager = midi_manager
        
        # Playback state
        self.is_playing = False
        self._play_on_load = False
        self.playback_start_time = 0
        self.current_timeline_time = 0
        self.last_pause_time = 0
        self.audio_offset_sec = 0.0
        
        # Current files
        self.midi_file: Optional[str] = None
        self.mp3_file: Optional[str] = None
        self.port_name: Optional[str] = None
        
        # Setup media player
        self.media_player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.audio_output.setVolume(audio_volume)
        self.media_player.setAudioOutput(self.audio_output)
        self.media_player.errorOccurred.connect(self._on_media_error)
        self.media_player.mediaStatusChanged.connect(self._on_media_status_changed)
        
        # Playback timer (50fps update rate)
        self.playback_timer = QTimer(self)
        self.playback_timer.setInterval(20)
        self.playback_timer.timeout.connect(self._on_playback_tick)
        
        self.logger.info("Media controller initialized")
    
    def set_files(self, mp3_file: Optional[str], midi_file: Optional[str]) -> None:
        """Set the media files for playback."""
        self.mp3_file = mp3_file
        self.midi_file = midi_file
        
        if mp3_file:
            try:
                # Validate file exists and is accessible
                if not os.path.exists(mp3_file):
                    self.error_occurred.emit("File not found", mp3_file)
                    return
                
                self.logger.info(f"MP3 file set: {mp3_file}")
                # Load the MP3 file into the media player
                self.media_player.setSource(QUrl.fromLocalFile(mp3_file))
                
            except Exception as e:
                self.logger.error(f"Error setting MP3 file: {e}")
                self.error_occurred.emit(str(e), mp3_file)
                return
                
        if midi_file:
            try:
                if not os.path.exists(midi_file):
                    self.error_occurred.emit("MIDI file not found", midi_file)
                    return
                    
                self.logger.info(f"MIDI file set: {midi_file}")
                
            except Exception as e:
                self.logger.error(f"Error setting MIDI file: {e}")
                self.error_occurred.emit(str(e), midi_file)
    
    def set_midi_port(self, port_name: str) -> None:
        """Set the MIDI output port."""
        self.port_name = port_name
        self.logger.info(f"MIDI port set: {port_name}")
        
        # Switch MIDI port if currently playing
        if self.is_playing and self.midi_file:
            self.midi_manager.switch_port(port_name)
    
    def set_audio_volume(self, volume: float) -> None:
        """Set audio output volume (0.0 to 1.0)."""
        self.audio_output.setVolume(volume)
        self.logger.debug(f"Audio volume set to {volume}")
    
    def set_audio_muted(self, muted: bool) -> None:
        """Mute or unmute audio output."""
        self.audio_output.setMuted(muted)
        self.logger.debug(f"Audio muted: {muted}")
    
    def set_audio_offset(self, offset_sec: float) -> None:
        """Set audio offset relative to timeline."""
        self.audio_offset_sec = offset_sec
        self.logger.debug(f"Audio offset set to {offset_sec:.3f}s")
    
    def start_playback(self) -> bool:
        """Start media playback from the beginning."""
        if not self.mp3_file:
            self.logger.warning("Cannot start playback: no MP3 file loaded")
            return False
        
        self.logger.info("Starting fresh media playback")
        
        # Stop any current playback
        if self.is_playing:
            self.playback_timer.stop()
            self.media_player.pause()
        
        # Reset timeline position
        self.current_timeline_time = 0
        self.last_pause_time = 0
        self.is_playing = False
        
        # Start MIDI worker if we have MIDI file and port
        if self.midi_file and self.port_name:
            midi_started = self.midi_manager.start_worker(self.midi_file, self.port_name)
            if not midi_started:
                self.logger.warning("Failed to start MIDI worker, continuing with audio only")
        
        # Ensure media is loaded and reset position
        if not self.media_player.source().isValid():
            self.media_player.setSource(QUrl.fromLocalFile(self.mp3_file))
        self.media_player.setPosition(0)
        
        # Start playback immediately
        self.is_playing = True
        self.playback_start_time = time.time()
        self.playback_timer.start()
        self.media_player.play()
        self.playback_state_changed.emit(True)
        
        return True
    
    def play_pause_playback(self) -> None:
        """Toggle play/pause state."""
        if not self.mp3_file:
            self.logger.warning("Cannot play/pause: no MP3 file loaded")
            return
            
        if self.is_playing:
            self._pause_playback()
        else:
            # If we haven't started yet or media player has no source, start from beginning
            if self.current_timeline_time == 0 or not self.media_player.source().isValid():
                self.start_playback()
            else:
                self._resume_playback()
    
    def _pause_playback(self) -> None:
        """Pause playback."""
        if not self.is_playing:
            return
            
        # Capture the current timeline position
        self.current_timeline_time = time.time() - self.playback_start_time
        
        self.logger.info(f"Pausing playback at {self.current_timeline_time:.2f}s")
        self.is_playing = False
        self.last_pause_time = time.time()
        self.playback_timer.stop()
        self.media_player.pause()
        self.playback_state_changed.emit(False)
    
    def _resume_playback(self) -> None:
        """Resume playback from current position."""
        if self.is_playing:
            return
            
        # If no media loaded yet, can't resume
        if not self.mp3_file or not self.media_player.source().isValid():
            self.logger.warning("Cannot resume: no media loaded")
            return
        
        # Check if we need to restart MIDI worker after stop
        if self.midi_file and self.port_name and not self.midi_manager.is_running() and self.last_pause_time == 0:
            self.logger.info("Restarting MIDI worker after stop")
            if not self.midi_manager.start_worker(self.midi_file, self.port_name):
                self.logger.warning("Failed to restart MIDI worker, continuing with audio only")
        
        self.logger.info(f"Resuming playback from {self.current_timeline_time:.2f}s")
        self.is_playing = True
        
        if self.last_pause_time > 0:
            # Resume from pause - adjust start time to account for pause duration
            self.playback_start_time = time.time() - self.current_timeline_time
            self.logger.debug(f"Resuming from pause at {self.current_timeline_time:.2f}s")
        else:
            # First play - start from beginning or current position
            if self.current_timeline_time == 0:
                self.playback_start_time = time.time()
            else:
                self.playback_start_time = time.time() - self.current_timeline_time
            self.logger.debug(f"Starting playback from {self.current_timeline_time:.2f}s")
        
        # Position MIDI if available
        if self.midi_file and self.current_timeline_time > 0:
            self.midi_manager.seek(self.current_timeline_time)
            self.logger.debug(f"Seeking MIDI to {self.current_timeline_time:.2f}s")
        
        # Set media player position
        audio_time_sec = max(0, self.current_timeline_time - self.audio_offset_sec)
        target_position_ms = int(audio_time_sec * 1000)
        self.media_player.setPosition(target_position_ms)
        self.logger.debug(f"Set media player position to {target_position_ms}ms")
        
        # Reset pause state and start timers
        self.last_pause_time = 0
        self.playback_timer.start()
        
        # Start audio playback
        self.media_player.play()
        self.playback_state_changed.emit(True)
    
    def stop_playback(self) -> None:
        """Stop all playback and reset to beginning."""
        self.logger.info("Stopping playback")
        self.is_playing = False
        
        # Stop timers and media
        self.playback_timer.stop()
        self.media_player.stop()
        
        # Stop MIDI if running
        if self.midi_manager.is_running():
            self.midi_manager.stop_worker()
        
        # Reset position to beginning
        self.current_timeline_time = 0
        self.last_pause_time = 0
        self.playback_start_time = 0
        
        # Reset media player position
        self.media_player.setPosition(0)
        
        self.playback_state_changed.emit(False)
        self.position_changed.emit(0)
    
    def seek_to_position(self, time_seconds: float) -> None:
        """Seek to a specific time position."""
        self.logger.debug(f"Seeking to {time_seconds:.2f}s")
        self.current_timeline_time = time_seconds
        self.playback_start_time = time.time() - time_seconds
        
        # Seek MIDI
        self.midi_manager.seek(time_seconds)
        
        # Update immediately
        self._on_playback_tick()
    
    def get_current_position(self) -> float:
        """Get current playback position in seconds."""
        return self.current_timeline_time
    
    def get_playback_state(self) -> bool:
        """Get current playback state."""
        return self.is_playing
    
    def _on_media_status_changed(self, status) -> None:
        """Handle media player status changes."""
        if status == QMediaPlayer.MediaStatus.LoadedMedia:
            self.logger.debug("Media loaded successfully")
            self.media_loaded.emit()
        elif status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.logger.info("Reached end of media")
            self.stop_playback()
    
    def _on_media_error(self, error, error_string: str) -> None:
        """Handle media player errors."""
        self.logger.error(f"Media player error: {error_string}")
        file_path = self.mp3_file or "Unknown file"
        self.error_occurred.emit(error_string, file_path)
    
    def _on_playback_tick(self) -> None:
        """Update playback position and synchronize media."""
        if not self.is_playing:
            return
        
        # Update timeline position
        self.current_timeline_time = time.time() - self.playback_start_time
        self.position_changed.emit(self.current_timeline_time)
        
        # Send MIDI data if available
        if self.midi_file and self.midi_manager.is_running():
            self.midi_manager.play_until(self.current_timeline_time)
        
        # Synchronize audio playback
        self._sync_audio_playback()
    
    def _sync_audio_playback(self) -> None:
        """Synchronize audio playback with timeline."""
        audio_time_sec = self.current_timeline_time - self.audio_offset_sec
        
        if audio_time_sec < 0:
            # Audio should not be playing yet
            if self.media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self.media_player.pause()
        else:
            # Audio should be playing
            if self.media_player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
                self.media_player.play()
            
            # Check if audio sync is off and correct it
            current_audio_pos_ms = self.media_player.position()
            target_audio_pos_ms = audio_time_sec * 1000
            
            # 150ms tolerance for resync to avoid jitter
            if abs(current_audio_pos_ms - target_audio_pos_ms) > 150:
                self.media_player.setPosition(int(target_audio_pos_ms))
                self.logger.debug(f"Audio resynced: {target_audio_pos_ms:.0f}ms")
    
    def cleanup(self) -> None:
        """Clean up resources."""
        self.logger.info("Cleaning up media controller")
        self.stop_playback()
        self.midi_manager.cleanup()