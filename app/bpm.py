# ABOUTME: BpmState — the logic-level holder of the session BPM. Widgets are
# ABOUTME: views of it; the .mid's set_tempo meta stays canonical on disk.
from PyQt6.QtCore import QObject, pyqtSignal


class BpmState(QObject):
    """Single runtime source of truth for the current song's BPM.

    Every consumer (metronome, count-in sizing, show autosave, web remote,
    toolbar spinbox/chip) subscribes to bpm_changed; every setter funnels
    through set_bpm, which clamps to the musical range and no-ops on equal
    values — so view-sync loops (spinbox → state → spinbox) settle
    immediately instead of ping-ponging.
    """

    bpm_changed = pyqtSignal(int)

    def __init__(self, initial: int = 120):
        super().__init__()
        self._bpm = self._clamp(initial)

    @staticmethod
    def _clamp(value) -> int:
        return max(40, min(300, int(value)))

    def get_bpm(self) -> int:
        return self._bpm

    def set_bpm(self, value) -> None:
        value = self._clamp(value)
        if value == self._bpm:
            return
        self._bpm = value
        self.bpm_changed.emit(value)
