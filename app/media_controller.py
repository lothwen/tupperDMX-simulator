import os
import time
from typing import List, Optional, Tuple

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal, QTimer

from audio.engine import AudioEngine
from audio.mixer import BUS_BACKING
from tupperlogger import get_logger
from core.io_routing import MidiRouting
from midi import MidiManager

# Editor monitor: which track feeds the engine's backing bus. Session-only
# state — defaults to the backing every launch and is never persisted, so
# headless/web-remote/live use can never inherit a guide monitor.
MONITOR_BACKING = "backing"
MONITOR_GUIDE = "guide"


class MediaController(QObject):
    """Handles media playback control (backing audio + MIDI synchronization).

    The backing track plays through the shared AudioEngine (decoded PCM mixed
    with the click/voice buses); the master clock stays time.time()-based
    timeline time, with the engine's sample clock slaved to it exactly like
    the old QMediaPlayer was (pause while not yet entered, resync on drift).

    Count-in (Fase 4): when enabled for the song, start_playback() begins at a
    NEGATIVE timeline time (-bars of 4 beats); audio/MIDI wait for t >= 0 while
    the metronome runs through the boundary into the song."""

    # Signals
    playback_state_changed = pyqtSignal(bool)  # is_playing
    position_changed = pyqtSignal(float)  # current time in seconds
    media_loaded = pyqtSignal()
    error_occurred = pyqtSignal(str, str)  # error message, file_path
    monitor_changed = pyqtSignal(str)  # MONITOR_BACKING | MONITOR_GUIDE

    def __init__(self, midi_manager: MidiManager, engine: AudioEngine,
                 audio_volume: float = 0.5):
        super().__init__()
        self.logger = get_logger("app.media")
        self.midi_manager = midi_manager
        self.engine = engine

        # Playback state
        self.is_playing = False
        self._play_on_load = False
        self.playback_start_time = 0
        self.current_timeline_time = 0
        self.last_pause_time = 0
        self.audio_offset_sec = 0.0

        # Count-in state (fed from the song's precount + config.audio)
        self.count_in_enabled = False
        self.count_in_bars = 2
        self.bpm = 120

        # Voice cues: sorted [(time_sec, mono float32 samples)], fired when the
        # timeline crosses their time; pointer reset on stop/seek.
        self._voice_cues: List[Tuple[float, np.ndarray]] = []
        self._next_cue_index = 0

        # Current files. The guide is the song's optional full-mix reference;
        # the monitor picks which of the two the engine decodes and plays.
        self.audio_file: Optional[str] = None
        self.guide_file: Optional[str] = None
        self.guide_offset_sec = 0.0
        self.monitor = MONITOR_BACKING
        # Where playback sends MIDI; set from config.io by MainWindow.
        self.midi_routing = MidiRouting(to_controller=False, port=None)

        # Backing feed through the shared engine
        self.engine.set_bus_volume(BUS_BACKING, audio_volume)
        self.engine.backing_loaded.connect(self._on_backing_loaded)
        self.engine.backing_failed.connect(self._on_backing_failed)
        self.engine.finished.connect(self._on_backing_finished)

        # Playback timer (50fps update rate)
        self.playback_timer = QTimer(self)
        self.playback_timer.setInterval(20)
        self.playback_timer.timeout.connect(self._on_playback_tick)

        self.logger.info("Media controller initialized")

    def set_files(self, audio_file: Optional[str], midi_file: Optional[str] = None) -> None:
        """Set audio file for playback. MIDI files should be loaded via MidiManager."""
        self.audio_file = audio_file

        if audio_file:
            try:
                # Validate file exists and is accessible
                if not os.path.exists(audio_file):
                    self.error_occurred.emit("File not found", audio_file)
                    return

                self.logger.info(f"Audio file set: {audio_file}")
                # Decode the monitored track into the engine (async)
                self._load_active()

            except Exception as e:
                self.logger.error(f"Error setting audio file: {e}")
                self.error_occurred.emit(str(e), audio_file)
                return
        else:
            self._load_active()

        # MIDI files are now handled by MidiManager
        if midi_file:
            self.logger.info(f"MIDI file should be loaded via MidiManager.load_from_file(): {midi_file}")

    def set_guide(self, path: Optional[str], offset_sec: float = 0.0) -> None:
        """Set the song's guide track (full-mix design reference); None clears.
        Reloads the engine only while the guide is being monitored."""
        self.guide_file = path
        self.guide_offset_sec = float(offset_sec)
        if self.monitor == MONITOR_GUIDE:
            self._load_active()

    def set_guide_offset(self, offset_sec: float) -> None:
        """Set the guide's offset relative to the timeline."""
        self.guide_offset_sec = float(offset_sec)

    def set_monitor(self, which: str) -> None:
        """Select which track the editor hears (backing | guide). Session-only
        state; live surfaces never call this."""
        if which not in (MONITOR_BACKING, MONITOR_GUIDE) or which == self.monitor:
            return
        self.monitor = which
        self.logger.info(f"Monitor: {which}")
        self._load_active()
        self.monitor_changed.emit(which)

    def active_file(self) -> Optional[str]:
        """The monitored track: the guide when selected and set, else the
        backing (a missing guide silently falls back)."""
        if self.monitor == MONITOR_GUIDE and self.guide_file:
            return self.guide_file
        return self.audio_file

    def active_offset(self) -> float:
        """Timeline offset of the monitored track."""
        if self.monitor == MONITOR_GUIDE and self.guide_file:
            return self.guide_offset_sec
        return self.audio_offset_sec

    def _load_active(self) -> None:
        active = self.active_file()
        if active:
            self.engine.load_backing(active)
        else:
            self.engine.unload_backing()

    def set_midi_routing(self, routing: MidiRouting) -> None:
        """Set the MIDI output routing (controller and/or external port)."""
        self.midi_routing = routing
        self.midi_manager.set_midi_routing(routing)
        self.logger.info(f"MIDI routing set: {routing}")

        # Re-route live playback (mirrors the old switch-port behaviour).
        if self.is_playing and self.midi_manager.midi_data:
            self.midi_manager.start_playback()

    def set_audio_volume(self, volume: float) -> None:
        """Set audio output volume (0.0 to 1.0)."""
        self.engine.set_bus_volume(BUS_BACKING, volume)
        self.logger.debug(f"Audio volume set to {volume}")

    def set_audio_muted(self, muted: bool) -> None:
        """Mute or unmute audio output."""
        self.engine.set_bus_muted(BUS_BACKING, muted)
        self.logger.debug(f"Audio muted: {muted}")

    def set_audio_offset(self, offset_sec: float) -> None:
        """Set audio offset relative to timeline."""
        self.audio_offset_sec = offset_sec
        self.logger.debug(f"Audio offset set to {offset_sec:.3f}s")

    def set_bpm(self, bpm: int) -> None:
        """Track the song BPM (needed to size the count-in)."""
        self.bpm = max(40, min(300, bpm))

    def set_count_in(self, enabled: bool, bars: int) -> None:
        """Configure the count-in for the current song (per-song precount
        on/off + the machine-level bar count)."""
        self.count_in_enabled = bool(enabled)
        self.count_in_bars = max(1, min(8, int(bars)))

    def count_in_duration(self) -> float:
        """Length of the configured count-in in seconds (bars of 4 beats)."""
        return self.count_in_bars * 4 * 60.0 / self.bpm

    def set_voice_cues(self, cues: List[Tuple[float, np.ndarray]]) -> None:
        """Install the song's rendered voice cues [(time_sec, samples)]."""
        self._voice_cues = sorted(cues, key=lambda c: c[0])
        self._reset_cue_pointer(self.current_timeline_time)

    def _reset_cue_pointer(self, position: float) -> None:
        """Point at the first cue at/after `position` (cues behind us never fire)."""
        self._next_cue_index = 0
        while (self._next_cue_index < len(self._voice_cues)
               and self._voice_cues[self._next_cue_index][0] < position):
            self._next_cue_index += 1

    def _fire_voice_cues(self) -> None:
        while (self._next_cue_index < len(self._voice_cues)
               and self._voice_cues[self._next_cue_index][0] <= self.current_timeline_time):
            _, samples = self._voice_cues[self._next_cue_index]
            self.engine.play_voice(samples)
            self._next_cue_index += 1

    def start_playback(self) -> bool:
        """Start media playback from the beginning (through the count-in when
        the song has one)."""
        if not self.active_file():
            self.logger.warning("Cannot start playback: no audio file loaded")
            return False

        self.logger.info("Starting fresh media playback")

        # Stop any current playback
        if self.is_playing:
            self.playback_timer.stop()
            self.engine.pause()

        # Reset timeline position — negative when counting in
        if self.count_in_enabled:
            self.current_timeline_time = -self.count_in_duration()
        else:
            self.current_timeline_time = 0
        self.last_pause_time = 0
        self.is_playing = False

        # Start MIDI playback if we have MIDI data and somewhere to send it
        if self.midi_manager.midi_data and self.midi_routing.has_output():
            midi_started = self.midi_manager.start_playback()
            if not midi_started:
                self.logger.warning("Failed to start MIDI playback, continuing with audio only")

        # Ensure the monitored track is (being) loaded and reset its position
        if not self.engine.is_backing_loaded():
            self.engine.load_backing(self.active_file())
        self.engine.set_backing_position(0)
        self._reset_cue_pointer(self.current_timeline_time)

        # Start playback immediately
        self.is_playing = True
        self.playback_start_time = time.time() - self.current_timeline_time
        self.playback_timer.start()
        self._sync_audio_playback()
        self.playback_state_changed.emit(True)

        return True

    def play_pause_playback(self) -> None:
        """Toggle play/pause state."""
        if not self.active_file():
            self.logger.warning("Cannot play/pause: no audio file loaded")
            return

        if self.is_playing:
            # Pausing mid count-in makes no musical sense: reset to the top.
            if self.current_timeline_time < 0:
                self.stop_playback()
            else:
                self._pause_playback()
        else:
            # If we haven't started yet, start from beginning
            if self.current_timeline_time == 0 or not self.engine.is_backing_loaded():
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
        self.engine.pause()
        self.playback_state_changed.emit(False)

    def _resume_playback(self) -> None:
        """Resume playback from current position."""
        if self.is_playing:
            return

        # If no media loaded yet, can't resume
        if not self.active_file() or not self.engine.is_backing_loaded():
            self.logger.warning("Cannot resume: no media loaded")
            return

        # Check if we need to restart MIDI playback after stop
        if self.midi_manager.midi_data and self.midi_routing.has_output() and not self.midi_manager.is_running() and self.last_pause_time == 0:
            self.logger.info("Restarting MIDI playback after stop")
            if not self.midi_manager.start_playback():
                self.logger.warning("Failed to restart MIDI playback, continuing with audio only")

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
        if self.midi_manager.midi_data and self.current_timeline_time > 0:
            self.midi_manager.seek(self.current_timeline_time)
            self.logger.debug(f"Seeking MIDI to {self.current_timeline_time:.2f}s")

        # Set backing position
        audio_time_sec = max(0, self.current_timeline_time - self.active_offset())
        self.engine.set_backing_position(audio_time_sec)
        self._reset_cue_pointer(self.current_timeline_time)

        # Reset pause state and start timers
        self.last_pause_time = 0
        self.playback_timer.start()

        # Start audio playback
        self._sync_audio_playback()
        self.playback_state_changed.emit(True)

    def stop_playback(self) -> None:
        """Stop all playback and reset to beginning."""
        self.logger.info("Stopping playback")
        self.is_playing = False

        # Stop timers and media
        self.playback_timer.stop()
        self.engine.pause()
        self.engine.stop_voices()

        # Stop MIDI if running
        if self.midi_manager.is_running():
            self.midi_manager.stop_worker()

        # Reset position to beginning
        self.current_timeline_time = 0
        self.last_pause_time = 0
        self.playback_start_time = 0

        # Reset backing position
        self.engine.set_backing_position(0)
        self._reset_cue_pointer(0)

        self.playback_state_changed.emit(False)
        self.position_changed.emit(0)

    def seek_to_position(self, time_seconds: float) -> None:
        """Seek to a specific time position (cancels a running count-in)."""
        self.logger.debug(f"Seeking to {time_seconds:.2f}s")
        self.current_timeline_time = time_seconds
        self.playback_start_time = time.time() - time_seconds

        # Seek MIDI
        self.midi_manager.seek(time_seconds)
        self._reset_cue_pointer(time_seconds)

        # Update immediately
        self._on_playback_tick()

    def get_current_position(self) -> float:
        """Get current playback position in seconds."""
        return self.current_timeline_time

    def get_playback_state(self) -> bool:
        """Get current playback state."""
        return self.is_playing

    def is_counting_in(self) -> bool:
        """True while playing the pre-song count-in (timeline time < 0)."""
        return self.is_playing and self.current_timeline_time < 0

    def _on_backing_loaded(self, path: str) -> None:
        """Backing decode finished (the engine's async load)."""
        if path == self.active_file():
            self.logger.debug("Media loaded successfully")
            self.media_loaded.emit()

    def _on_backing_finished(self) -> None:
        """Backing ran past its end while playing."""
        self.logger.info("Reached end of media")
        self.stop_playback()

    def _on_backing_failed(self, error_string: str, path: str) -> None:
        """Handle backing decode errors."""
        self.logger.error(f"Audio engine error: {error_string}")
        self.error_occurred.emit(error_string, path or self.active_file() or "Unknown file")

    def _on_playback_tick(self) -> None:
        """Update playback position and synchronize media."""
        if not self.is_playing:
            return

        # Update timeline position
        self.current_timeline_time = time.time() - self.playback_start_time
        self.position_changed.emit(self.current_timeline_time)

        # Send MIDI data if available (nothing fires while counting in: all
        # events live at t >= 0)
        if self.midi_manager.midi_data and self.midi_manager.is_running():
            self.midi_manager.play_until(self.current_timeline_time)

        # Fire voice cues the timeline just crossed
        self._fire_voice_cues()

        # Synchronize audio playback
        self._sync_audio_playback()

    def _sync_audio_playback(self) -> None:
        """Synchronize audio playback with timeline."""
        audio_time_sec = self.current_timeline_time - self.active_offset()

        if audio_time_sec < 0:
            # Audio should not be playing yet (count-in or positive offset)
            if self.engine.is_active():
                self.engine.pause()
        else:
            # Audio should be playing
            if not self.engine.is_active():
                self.engine.play()

            # Check if audio sync is off and correct it
            current_audio_pos_sec = self.engine.backing_position()

            # 150ms tolerance for resync to avoid jitter
            if abs(current_audio_pos_sec - audio_time_sec) > 0.150:
                self.engine.set_backing_position(audio_time_sec)
                self.logger.debug(f"Audio resynced: {audio_time_sec * 1000:.0f}ms")

    def cleanup(self) -> None:
        """Clean up resources."""
        self.logger.info("Cleaning up media controller")
        self.stop_playback()
        self.midi_manager.cleanup()
