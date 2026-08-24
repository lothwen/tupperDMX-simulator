# ABOUTME: BpmState — the logic-level holder of the session BPM and the
# ABOUTME: song's tempo map. Widgets are views of it; the .mid stays canonical.
from PyQt6.QtCore import QObject, pyqtSignal

from core.tempo_map import TempoMap, clamp_bpm


class BpmState(QObject):
    """Single runtime source of truth for the current song's tempo.

    Every consumer (metronome, count-in sizing, show autosave, web remote,
    toolbar spinbox/chip) subscribes to bpm_changed; every setter funnels
    through set_bpm, which clamps to the musical range and no-ops on equal
    values — so view-sync loops (spinbox → state → spinbox) settle
    immediately instead of ping-ponging.

    A song may also carry tempo-change markers, and the two together ARE the
    song's tempo — so the TempoMap is built here rather than in each consumer:
    a map assembled independently by the metronome and by the ruler is a map
    that can disagree with itself. `bpm` stays the BASE tempo, the one the .mid
    persists as its single set_tempo; the markers ride the show manifest.
    """

    bpm_changed = pyqtSignal(int)
    tempo_map_changed = pyqtSignal(object)  # TempoMap

    def __init__(self, initial: int = 120):
        super().__init__()
        self._bpm = self._clamp(initial)
        self._changes = []
        self._map = TempoMap(self._bpm)

    @staticmethod
    def _clamp(value) -> int:
        return clamp_bpm(value)

    def get_bpm(self) -> int:
        """The BASE tempo — what the song starts at and the .mid persists."""
        return self._bpm

    def set_bpm(self, value) -> None:
        value = self._clamp(value)
        if value == self._bpm:
            return
        self._bpm = value
        self._rebuild()
        self.bpm_changed.emit(value)

    def tempo_map(self) -> TempoMap:
        """Base tempo + the song's tempo markers, as one map."""
        return self._map

    def set_tempo_changes(self, changes) -> None:
        """Install the current song's tempo markers as [(seconds, bpm)].

        Always rebuilds and re-announces, even for an unchanged list: this is
        called on every song load, and the previous song's map must not survive
        into a song whose markers happen to match nothing."""
        self._changes = [(float(t), self._clamp(b)) for t, b in changes]
        self._rebuild()

    def _rebuild(self) -> None:
        self._map = TempoMap(self._bpm, self._changes)
        self.tempo_map_changed.emit(self._map)
