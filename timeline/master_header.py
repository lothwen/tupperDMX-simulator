"""
The block at the head of the timeline's track strip.

A "TRACKS" title with the live fixture count over the MASTER (audio), CLICK
(metronome) and VOICE (TTS guide) volume rows — the three output buses, each a
label, an M mute toggle, a level slider and a dB read-out.

It owns those controls and speaks for them: every edit leaves as one of the
six signals below, which the timeline re-emits as its own, so the window still
hears one object.
"""

import math

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QSlider,
                             QVBoxLayout, QWidget)


def format_db(percent: int) -> str:
    """Render a 0-100 volume percentage as an approximate dB readout
    (100% = 0 dB, 0% = -∞)."""
    if percent <= 0:
        return "-∞"
    db = 20.0 * math.log10(percent / 100.0)
    return "0dB" if db >= -0.5 else f"{round(db)}dB"


class MasterHeaderPanel(QWidget):
    """The strip header: the TRACKS counter over the three bus volume rows."""

    audio_volume_changed = pyqtSignal(int)
    audio_mute_toggled = pyqtSignal(bool)
    metronome_volume_changed = pyqtSignal(int)
    metronome_mute_toggled = pyqtSignal(bool)
    voice_volume_changed = pyqtSignal(int)
    voice_mute_toggled = pyqtSignal(bool)

    def __init__(self, audio_volume, metronome_volume, voice_volume,
                 width, parent=None):
        super().__init__(parent)
        self.setObjectName("TrackHeaderPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(width)

        # MASTER = audio output volume, drawn with a green→orange level gradient.
        (self.audio_volume_slider, self.audio_mute_button,
         self._audio_db_label) = self._volume_controls(
            audio_volume, "MasterVolumeSlider", "AudioMuteButton",
            self.audio_volume_changed, self.audio_mute_toggled)

        # METRO = metronome volume (kept here so the control isn't lost; the
        # metronome mute also lives on the toolbar).
        (self.metronome_volume_slider, self.metronome_mute_button,
         self._metro_db_label) = self._volume_controls(
            metronome_volume, "MetroVolumeSlider", "MetronomeMuteButton",
            self.metronome_volume_changed, self.metronome_mute_toggled)

        # VOICE = TTS guide bus (voice cues + spoken count-in). It borrows the
        # metronome's object names, so the stylesheet draws it the same way.
        (self.voice_volume_slider, self.voice_mute_button,
         self._voice_db_label) = self._volume_controls(
            voice_volume, "MetroVolumeSlider", "MetronomeMuteButton",
            self.voice_volume_changed, self.voice_mute_toggled)

        # "TRACKS" title row with the live fixture counter.
        tracks_title = QLabel("TRACKS")
        tracks_title.setObjectName("TracksTitle")
        self.track_count_label = QLabel()
        self.track_count_label.setObjectName("TracksCount")
        self.track_count_label.setAlignment(Qt.AlignmentFlag.AlignRight
                                            | Qt.AlignmentFlag.AlignVCenter)
        tracks_row = QHBoxLayout()
        tracks_row.setContentsMargins(0, 0, 0, 0)
        tracks_row.addWidget(tracks_title)
        tracks_row.addStretch(1)
        tracks_row.addWidget(self.track_count_label)

        panel = QVBoxLayout(self)
        panel.setContentsMargins(8, 6, 8, 8)
        panel.setSpacing(4)
        panel.addLayout(tracks_row)
        panel.addStretch(1)
        panel.addLayout(self._volume_row("MASTER", self.audio_mute_button,
                                         self.audio_volume_slider, self._audio_db_label))
        panel.addLayout(self._volume_row("CLICK", self.metronome_mute_button,
                                         self.metronome_volume_slider, self._metro_db_label))
        panel.addLayout(self._volume_row("VOICE", self.voice_mute_button,
                                         self.voice_volume_slider, self._voice_db_label))
        panel.addStretch(1)

    def _volume_controls(self, initial, slider_name, mute_name,
                         volume_signal, mute_signal):
        """One bus's (slider, mute toggle, dB readout), wired to its signals."""
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setObjectName(slider_name)
        slider.setRange(0, 100)
        slider.setValue(int(initial * 100))

        mute_button = QPushButton("M")
        mute_button.setObjectName(mute_name)
        mute_button.setCheckable(True)
        mute_button.setFixedSize(18, 18)

        db_label = QLabel(format_db(slider.value()))
        db_label.setObjectName("VolumeDbLabel")
        db_label.setAlignment(Qt.AlignmentFlag.AlignRight
                              | Qt.AlignmentFlag.AlignVCenter)

        slider.valueChanged.connect(volume_signal.emit)
        slider.valueChanged.connect(
            lambda value: db_label.setText(format_db(value)))
        mute_button.toggled.connect(mute_signal.emit)
        return slider, mute_button, db_label

    def _volume_row(self, label_text, mute_button, slider, db_label) -> QHBoxLayout:
        """One labeled volume row: title, mute toggle, slider, dB readout."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        label = QLabel(label_text)
        label.setObjectName("VolumeRowLabel")
        row.addWidget(label)
        row.addWidget(mute_button)
        row.addWidget(slider, 1)
        row.addWidget(db_label)
        return row

    def set_track_count(self, count: int) -> None:
        """Refresh the TRACKS counter (live fixture count)."""
        self.track_count_label.setText(str(count))

    def set_volumes(self, audio=None, metronome=None, voice=None) -> None:
        """Move the sliders to volumes that changed elsewhere (a live userprefs
        reload). Signals are blocked: these values have already been applied to
        the buses, and re-emitting them as user edits would bounce straight
        back out as another save."""
        for value, slider, db_label in (
                (audio, self.audio_volume_slider, self._audio_db_label),
                (metronome, self.metronome_volume_slider, self._metro_db_label),
                (voice, self.voice_volume_slider, self._voice_db_label)):
            if value is None:
                continue
            blocked = slider.blockSignals(True)
            slider.setValue(int(round(float(value) * 100)))
            slider.blockSignals(blocked)
            db_label.setText(format_db(slider.value()))
