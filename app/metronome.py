# ABOUTME: Metronome — schedules the click into the output: beats from
# ABOUTME: core/click.py, placed at exact output frames ahead of the playhead.
import time
from PyQt6.QtCore import QObject, Qt, pyqtSignal, QTimer

from audio.engine import AudioEngine
from audio.mixer import BUS_CLICK
from core import click
from core.tempo_map import TempoMap
from tupperlogger import get_logger

# How far ahead of the output playhead beats are queued. It only has to exceed
# the longest stall the Qt thread can suffer, and this app's stage was measured
# blocking it for up to 129 ms; half a second is comfortable margin without
# queueing so far ahead that a stop leaves audible beeps behind.
LOOKAHEAD_S = 0.5
# How often the queue is refilled. Its jitter does not reach the click — that
# is the entire point — so it is chosen to keep the queue topped up cheaply.
PUMP_MS = 25
# How far behind the playhead a beat may be and still sound, placed at the
# playhead. Without it, ANY lag between reading the transport's position and
# taking the anchor — microseconds are enough — makes the beat sitting on that
# position count as passed, and the count-in loses its opening DOWNBEAT. 20 ms
# is below the flam a musician can hear and far above that lag; a beat later
# than this is still dropped, which is what keeps a stall from crowding beats
# in behind the playhead.
START_GRACE_S = 0.02


class Metronome(QObject):
    """The click's scheduler: the click is SCHEDULED INTO THE OUTPUT, not played by
    a timer. Beat times come from the song's TempoMap, are converted to
    absolute output frames, and are handed to the mixer up to LOOKAHEAD_S
    ahead of the playhead; the mixer places each beep at its exact sample.

    That inversion is what makes the click deterministic. A timer-driven beep
    lands wherever the Qt thread happened to be free, and this app's stage
    blocks that thread for 46-129 ms at a time — so the beat moved with the
    graphics load. A scheduled beep is already in the buffer before the stall
    starts; the pump can be late by anything up to the lookahead and the
    listener hears nothing of it.

    What can still go wrong is scheduling too LATE — a pump that misses its
    window past the playhead. That is what `min lead` in the timing summary
    watches, and beats whose frame is already behind the output are dropped
    rather than played late, because a click that resumes late misleads a
    musician where a missing one merely disappoints.

    WHAT each beat is — its time, its 1-4 number, the kind of sound it takes —
    is `core.click`'s answer, and whether the click runs at a position at all
    is `follow()`'s, so this class only has to get beats to the output on time.
    Negative timeline positions (the count-in) need no casing: the beat index
    runs seamlessly through t=0 into the song."""

    # Signals
    metronome_volume_changed = pyqtSignal(float)  # volume 0.0-1.0
    metronome_mute_toggled = pyqtSignal(bool)  # muted state
    # The output has REACHED this beat (UI, web remote). Lags scheduling by
    # the lookahead, which is what keeps a beat indicator honest.
    beat_occurred = pyqtSignal(int)  # beat number
    # A beat has just been QUEUED: (beat number, its timeline time, its output
    # frame). Anything that must sound WITH the beat — the spoken count-in —
    # schedules itself at that same frame rather than reacting later.
    beat_scheduled = pyqtSignal(int, float, int)
    audio_error_occurred = pyqtSignal(str, str)  # error message, error type

    def __init__(self, engine: AudioEngine, initial_volume: float = 0.5):
        super().__init__()
        self.logger = get_logger("app.metronome")
        self.engine = engine
        # Shared with the audio feed: one record of the whole timing picture.
        self.monitor = engine.monitor

        # Metronome state
        self.tempo_map = TempoMap(120)
        self.beat_count = 0
        # Continuous beat index of the beat scheduled next (negative through a
        # count-in). The map turns it into both a wall-clock time and a downbeat.
        self._beat_index = 0
        self.is_metronome_enabled = False
        self.metronome_last_volume = initial_volume
        self.metronome_is_muted = False
        # The current song's control markers — the state a stop-click leaves
        # behind, which follow() re-resolves rather than replays.
        self._control_markers = []
        # The grid's anchor: timeline time `_anchor_time` falls at output frame
        # `_anchor_frame`. Every beat's frame is derived from this pair, so the
        # click rides the sound card's clock rather than the system's.
        self._anchor_frame = 0
        self._anchor_time = 0.0
        # Beats queued but not yet reached by the output: [(frame, number)].
        self._pending = []
        # The first beat after a start is placed AT the playhead by design, so
        # its lead is zero for a reason that says nothing about the pump. It is
        # left out of the lead metric, which exists to catch the pump running
        # late — otherwise every start would peg that metric at zero.
        self._opening_beat = True

        self.engine.set_bus_volume(BUS_CLICK, initial_volume)

        # The pump: refills the queue, and reports beats the output has
        # reached. Precise only so it keeps a tidy cadence — its jitter is
        # absorbed by the lookahead and never reaches a beep.
        self.metronome_timer = QTimer(self)
        self.metronome_timer.setInterval(PUMP_MS)
        self.metronome_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.metronome_timer.timeout.connect(self._pump)

        self.logger.info("Metronome initialized")

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

    def set_control_markers(self, markers) -> None:
        """Install the current song's control markers (what follow() asks)."""
        self._control_markers = list(markers)

    def follow(self, position: float, anchor_monotonic: float = None) -> None:
        """Put the click where the transport is: running from `position`,
        unless a stop-click marker has already silenced it there.

        Every start, seek and retime goes through here, because a stop-click
        outlives its own crossing — landing before the marker brings the click
        back, landing past it does not. `anchor_monotonic` is as for
        start_metronome()."""
        if click.click_stopped_at(self._control_markers, position):
            self.stop_metronome()
        else:
            self.start_metronome(position, anchor_monotonic)

    def start_metronome(self, timeline_position_sec: float = 0.0,
                        anchor_monotonic: float = None) -> None:
        """Start the click, phased so that `timeline_position_sec` falls where
        the output playhead is now.

        `anchor_monotonic` is the monotonic instant at which that position was
        the transport's — `MediaController.monotonic_at()`. A position and
        "now" are not the same moment: on a seek the transport has since run a
        whole tick (position signal to the UI, MIDI dispatch), and anchoring on
        "now" would sit the whole grid that far behind the music. None means
        the caller read the position just now."""
        self.stop_metronome()
        self.is_metronome_enabled = True

        lag = (0.0 if anchor_monotonic is None
               else max(0.0, time.monotonic() - float(anchor_monotonic)))
        self._anchor_time = timeline_position_sec + lag
        self._anchor_frame = self.engine.frames_rendered()

        # The first beat at or after the anchor, less the grace — so the beat
        # the transport is sitting ON still counts as due.
        self._set_beat_index(click.first_index_at(
            self.tempo_map, self._anchor_time - START_GRACE_S))
        self._opening_beat = True

        self._pump()   # queue the opening beats before returning
        self.metronome_timer.start()
        self.logger.info(
            f"Metronome started at {self.tempo_map.bpm_at(self._anchor_time)} "
            f"BPM, position {timeline_position_sec:.2f}s, "
            f"{len(self._pending)} beat(s) queued")

    def stop_metronome(self) -> None:
        """Stop the click and un-queue the beats already placed ahead of the
        playhead — they belong to a grid that is about to stop existing."""
        was_enabled = self.is_metronome_enabled
        self.is_metronome_enabled = False
        self.metronome_timer.stop()
        self.engine.clear_clicks()
        self._pending = []
        self.beat_count = 0
        self._beat_index = 0
        if was_enabled:
            self.logger.info("Metronome stopped")

    def _set_beat_index(self, index: int) -> None:
        """Move to a continuous beat index, deriving the 1-4 count from it."""
        self._beat_index = int(index)
        self.beat_count = click.beat_number(self._beat_index)

    def _frame_at(self, seconds: float) -> int:
        """The output frame timeline time `seconds` falls on.

        The beat's time comes from the tempo map, so a tempo marker retimes
        the click from that marker on; expressed in frames, the answer rides
        the sound card's clock instead of the system's."""
        return self._anchor_frame + round(
            (seconds - self._anchor_time) * self.engine.rate())

    def _pump(self) -> None:
        """Queue every beat now inside the lookahead, and report those the
        output has reached. Late by any amount up to LOOKAHEAD_S is harmless:
        the beeps for that window are already in the buffer."""
        if not self.is_metronome_enabled:
            return
        rendered = self.engine.frames_rendered()
        horizon = rendered + int(LOOKAHEAD_S * self.engine.rate())

        grace = int(START_GRACE_S * self.engine.rate())
        dropped = 0
        while True:
            event = click.event_at(self.tempo_map, self._beat_index)
            frame = self._frame_at(event.seconds)
            if frame >= horizon:
                break
            if frame < rendered - grace:
                # The playhead is well past it. Dropping keeps the grid;
                # sounding it now would put a beat between two beats.
                dropped += 1
            else:
                late = self.engine.schedule_click(event.kind, at_frame=frame)
                if self._opening_beat:
                    self._opening_beat = False
                else:
                    self.monitor.record_lead(
                        (frame - rendered) / max(1, self.engine.rate()))
                self.monitor.record_beat(late / max(1, self.engine.rate()))
                self.beat_scheduled.emit(event.number, event.seconds, frame)
                self._pending.append((frame, event.number))
            self._set_beat_index(self._beat_index + 1)

        if dropped:
            self.monitor.record_dropped(dropped, 0.0)

        # Beats the output has now reached: the moment a listener hears them.
        while self._pending and self._pending[0][0] <= rendered:
            _frame, number = self._pending.pop(0)
            self.beat_occurred.emit(number)

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
        self.logger.info("Cleaning up metronome")
        self.stop_metronome()
