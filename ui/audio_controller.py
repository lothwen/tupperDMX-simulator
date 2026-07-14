import math
import time
from PyQt6.QtCore import QObject, pyqtSignal, QTimer

from audio.engine import AudioEngine
from audio.mixer import BUS_CLICK
from tupperlogger import get_logger


class AudioController(QObject):
    """Metronome engine: drift-anchored beat scheduling. The beeps themselves
    play on the shared AudioEngine's click bus (so they follow the click/voice
    channel routing); this class owns WHEN they fire.

    Supports negative timeline positions (the Fase-4 count-in): beat math uses
    floor(), so a metronome started at -N beats runs seamlessly through t=0
    into the song."""

    # Signals
    metronome_volume_changed = pyqtSignal(float)  # volume 0.0-1.0
    metronome_mute_toggled = pyqtSignal(bool)  # muted state
    beat_occurred = pyqtSignal(int)  # beat number
    audio_error_occurred = pyqtSignal(str, str)  # error message, error type

    def __init__(self, engine: AudioEngine, initial_volume: float = 0.5):
        super().__init__()
        self.logger = get_logger("ui.audio")
        self.engine = engine

        # Metronome state
        self.bpm = 120
        self.beat_count = 0
        self.is_metronome_enabled = False
        self.metronome_last_volume = initial_volume
        self.metronome_is_muted = False
        self.next_beat_expected_time = 0.0

        self.engine.set_bus_volume(BUS_CLICK, initial_volume)

        # Metronome timer
        self.metronome_timer = QTimer(self)
        self.metronome_timer.timeout.connect(self._play_metronome_beep)

        self.logger.info("Audio controller initialized")

    def set_bpm(self, bpm: int) -> None:
        """Set metronome BPM."""
        self.bpm = max(40, min(300, bpm))  # Clamp between 40-300 BPM
        self.logger.debug(f"BPM set to {self.bpm}")

    def start_metronome(self, timeline_position_sec: float = 0.0) -> None:
        """Start the metronome synchronized to a specific timeline position
        (which may be negative during a count-in)."""
        self.is_metronome_enabled = True

        # Calculate beat position based on timeline
        beat_duration_sec = 60.0 / self.bpm
        # Calculate which beat we're approaching or just passed. floor() (not
        # int()) so negative positions truncate toward the earlier beat.
        beats_from_start = timeline_position_sec / beat_duration_sec
        current_beat_index = math.floor(beats_from_start)
        time_since_beat_boundary = (beats_from_start - current_beat_index) * beat_duration_sec

        if time_since_beat_boundary < 0.001:
            # We're exactly on a beat boundary, play that beat
            self.beat_count = (current_beat_index % 4) + 1
            time_since_last_beat = 0.0
        else:
            # We're past a beat boundary, that beat already happened
            # Prepare the next beat to be played
            next_beat_index = current_beat_index + 1
            self.beat_count = (next_beat_index % 4) + 1
            time_since_last_beat = time_since_beat_boundary

        # Time until next beat
        time_to_next_beat = beat_duration_sec - time_since_last_beat

        current_time = time.time()

        # Play beat immediately if we're on or very close to a beat boundary
        if time_since_beat_boundary < 0.001:  # Very close to beat boundary (within 1ms)
            # We're right on a beat, play it immediately
            self.next_beat_expected_time = current_time
            self._play_metronome_beep()
            self.logger.info(f"Metronome started at {self.bpm} BPM, position {timeline_position_sec:.2f}s, playing beat {self.beat_count} immediately")
        else:
            # Start timer to fire at the next beat
            self.next_beat_expected_time = current_time + time_to_next_beat
            initial_delay_ms = int(time_to_next_beat * 1000)
            self.metronome_timer.setSingleShot(True)
            self.metronome_timer.start(initial_delay_ms)
            self.logger.info(f"Metronome started at {self.bpm} BPM, position {timeline_position_sec:.2f}s, will play beat {self.beat_count} in {time_to_next_beat:.3f}s")

    def stop_metronome(self) -> None:
        """Stop the metronome."""
        self.is_metronome_enabled = False
        self.metronome_timer.stop()
        self.beat_count = 0
        self.logger.info("Metronome stopped")

    def _play_metronome_beep(self) -> None:
        """Play metronome beep sound."""
        if not self.is_metronome_enabled:
            return

        # High pitch on downbeat (beat 1), low pitch on other beats
        self.engine.trigger_click(downbeat=(self.beat_count == 1))

        self.beat_occurred.emit(self.beat_count)
        self.logger.debug(f"Metronome beat {self.beat_count}")

        # Advance to next beat
        self.beat_count = (self.beat_count % 4) + 1

        # Calculate next beat time to prevent drift
        beat_duration_sec = 60.0 / self.bpm
        self.next_beat_expected_time += beat_duration_sec

        # Schedule next beat
        current_time = time.time()
        delay_sec = self.next_beat_expected_time - current_time
        delay_ms = max(0, int(delay_sec * 1000))

        self.metronome_timer.setSingleShot(True)
        self.metronome_timer.start(delay_ms)

    def set_metronome_volume(self, volume: float) -> None:
        """Set metronome volume (0.0 to 1.0)."""
        volume = max(0.0, min(1.0, volume))
        self.metronome_last_volume = volume

        if not self.metronome_is_muted:
            self.engine.set_bus_volume(BUS_CLICK, volume)

        self.metronome_volume_changed.emit(volume)
        self.logger.debug(f"Metronome volume set to {volume:.2f}")

    def set_metronome_muted(self, muted: bool) -> None:
        """Mute or unmute the metronome."""
        self.metronome_is_muted = muted

        self.engine.set_bus_muted(BUS_CLICK, muted)
        if not muted:
            self.engine.set_bus_volume(BUS_CLICK, self.metronome_last_volume)

        self.metronome_mute_toggled.emit(muted)
        self.logger.debug(f"Metronome muted: {muted}")

    def get_metronome_volume(self) -> float:
        """Get current metronome volume."""
        return self.metronome_last_volume

    def is_metronome_muted(self) -> bool:
        """Check if metronome is muted."""
        return self.metronome_is_muted

    def is_metronome_running(self) -> bool:
        """Check if metronome is currently running."""
        return self.is_metronome_enabled

    def get_current_bpm(self) -> int:
        """Get current BPM setting."""
        return self.bpm

    def get_beat_count(self) -> int:
        """Get current beat count."""
        return self.beat_count

    def reset_beat_count(self) -> None:
        """Reset beat count to 0."""
        self.beat_count = 0
        self.logger.debug("Beat count reset")

    def cleanup(self) -> None:
        """Clean up audio resources."""
        self.logger.info("Cleaning up audio controller")
        self.stop_metronome()
