import math
import time
from PyQt6.QtCore import QObject, pyqtSignal, QTimer

from audio.engine import AudioEngine
from audio.mixer import BUS_CLICK
from core.tempo_map import BEATS_PER_BAR, TempoMap
from tupperlogger import get_logger


class AudioController(QObject):
    """Metronome engine: drift-anchored beat scheduling. The beeps themselves
    play on the shared AudioEngine's click bus (so they follow the click/voice
    channel routing); this class owns WHEN they fire.

    Supports negative timeline positions (the Fase-4 count-in): beat math uses
    floor(), so a metronome started at -N beats runs seamlessly through t=0
    into the song.

    WHEN a beat falls is asked of the song's TempoMap rather than computed from
    one BPM, so a tempo-change marker retimes the click from that marker on. The
    drift anchoring is unchanged — each beat still schedules the next off an
    accumulated wall-clock target — only the interval added to that target now
    comes from the map, and the beat INDEX (not a free-running counter) decides
    the downbeat, so the accent stays on beat 1 across a change."""

    # Signals
    metronome_volume_changed = pyqtSignal(float)  # volume 0.0-1.0
    metronome_mute_toggled = pyqtSignal(bool)  # muted state
    beat_occurred = pyqtSignal(int)  # beat number
    audio_error_occurred = pyqtSignal(str, str)  # error message, error type

    def __init__(self, engine: AudioEngine, initial_volume: float = 0.5):
        super().__init__()
        self.logger = get_logger("app.audio")
        self.engine = engine

        # Metronome state
        self.tempo_map = TempoMap(120)
        self.beat_count = 0
        # Continuous beat index of the beat scheduled next (negative through a
        # count-in). The map turns it into both a wall-clock time and a downbeat.
        self._beat_index = 0
        self.is_metronome_enabled = False
        self.metronome_last_volume = initial_volume
        self.metronome_is_muted = False
        self.next_beat_expected_time = 0.0

        self.engine.set_bus_volume(BUS_CLICK, initial_volume)

        # Metronome timer
        self.metronome_timer = QTimer(self)
        self.metronome_timer.timeout.connect(self._play_metronome_beep)

        self.logger.info("Audio controller initialized")

    @property
    def bpm(self) -> int:
        """The tempo the click starts a song at (the map's first segment)."""
        return self.tempo_map.bpm_at(0.0)

    def set_bpm(self, bpm: int) -> None:
        """Set a single metronome BPM (a map with no tempo changes)."""
        self.set_tempo_map(TempoMap(bpm))

    def set_tempo_map(self, tempo_map: TempoMap) -> None:
        """Install the song's tempo map. Restarting the click is the caller's
        call — AppCore restarts it at the current position when it is running,
        which is also what re-phases the beat after a mid-song tempo change."""
        self.tempo_map = tempo_map
        self.logger.debug(
            f"Tempo map set: base {tempo_map.bpm_at(0.0)} BPM, "
            f"{len(tempo_map.changes())} change(s)")

    def start_metronome(self, timeline_position_sec: float = 0.0) -> None:
        """Start the metronome synchronized to a specific timeline position
        (which may be negative during a count-in)."""
        self.is_metronome_enabled = True

        # Which beat are we approaching or just past? floor() (not int()) so
        # negative count-in positions truncate toward the EARLIER beat, which
        # is what makes them number correctly toward beat 1 at t=0.
        beat_index = math.floor(self.tempo_map.beat_at(timeline_position_sec))
        time_since_beat_boundary = (timeline_position_sec
                                    - self.tempo_map.seconds_at_beat(beat_index))

        current_time = time.time()
        bpm_here = self.tempo_map.bpm_at(timeline_position_sec)

        # Play beat immediately if we're on or very close to a beat boundary
        if time_since_beat_boundary < 0.001:  # Very close to beat boundary (within 1ms)
            # We're right on a beat, play it immediately
            self._set_beat_index(beat_index)
            self.next_beat_expected_time = current_time
            self._play_metronome_beep()
            self.logger.info(f"Metronome started at {bpm_here} BPM, position {timeline_position_sec:.2f}s, playing beat {self.beat_count} immediately")
        else:
            # That beat already happened; prepare the next one.
            self._set_beat_index(beat_index + 1)
            time_to_next_beat = (self.tempo_map.seconds_at_beat(self._beat_index)
                                 - timeline_position_sec)
            self.next_beat_expected_time = current_time + time_to_next_beat
            initial_delay_ms = int(time_to_next_beat * 1000)
            self.metronome_timer.setSingleShot(True)
            self.metronome_timer.start(initial_delay_ms)
            self.logger.info(f"Metronome started at {bpm_here} BPM, position {timeline_position_sec:.2f}s, will play beat {self.beat_count} in {time_to_next_beat:.3f}s")

    def _set_beat_index(self, index: int) -> None:
        """Move to a continuous beat index, deriving the 1-4 count from it.

        Python's % is floor-based, so a count-in's negative indices land on the
        same 1-4 cycle the song continues with — no separate count-in casing."""
        self._beat_index = int(index)
        self.beat_count = (self._beat_index % BEATS_PER_BAR) + 1

    def stop_metronome(self) -> None:
        """Stop the metronome."""
        self.is_metronome_enabled = False
        self.metronome_timer.stop()
        self.beat_count = 0
        self._beat_index = 0
        self.logger.info("Metronome stopped")

    def _play_metronome_beep(self) -> None:
        """Play metronome beep sound."""
        if not self.is_metronome_enabled:
            return

        # High pitch on downbeat (beat 1), low pitch on other beats
        self.engine.trigger_click(downbeat=(self.beat_count == 1))

        self.beat_occurred.emit(self.beat_count)
        self.logger.debug(f"Metronome beat {self.beat_count}")

        # Advance to next beat. The interval is the map's — across a tempo
        # marker the two beats it spans are different lengths.
        interval = (self.tempo_map.seconds_at_beat(self._beat_index + 1)
                    - self.tempo_map.seconds_at_beat(self._beat_index))
        self._set_beat_index(self._beat_index + 1)

        # Calculate next beat time to prevent drift
        self.next_beat_expected_time += interval

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

    def bpm_at(self, timeline_position_sec: float) -> int:
        """The tempo sounding at a timeline position (tempo markers applied)."""
        return self.tempo_map.bpm_at(timeline_position_sec)

    def get_beat_count(self) -> int:
        """Get current beat count."""
        return self.beat_count

    def reset_beat_count(self) -> None:
        """Reset beat count to 0."""
        self.beat_count = 0
        self._beat_index = 0
        self.logger.debug("Beat count reset")

    def cleanup(self) -> None:
        """Clean up audio resources."""
        self.logger.info("Cleaning up audio controller")
        self.stop_metronome()
