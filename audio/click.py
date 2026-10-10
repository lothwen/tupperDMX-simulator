# ABOUTME: What the click sounds like — one mono sample per click kind, rebuilt
# ABOUTME: at the output rate. Qt-free; the kinds come from core/click.py.

from typing import Dict

import numpy as np

from audio.mixer import SAMPLE_RATE
from core.click import KIND_ACCENT, KIND_BEAT

# Metronome beep frequencies in Hz. Hardcoded by design — tweak here if needed;
# intentionally not exposed via config or the Settings UI. High = downbeat
# (beat 1), low = the other beats.
METRONOME_HIGH_PITCH_HZ = 1200
METRONOME_LOW_PITCH_HZ = 800

_PITCH_HZ = {KIND_ACCENT: METRONOME_HIGH_PITCH_HZ,
             KIND_BEAT: METRONOME_LOW_PITCH_HZ}


def make_beep(freq_hz: float, duration_sec: float = 0.1,
              sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Mono float32 sine beep with a short fade to avoid edge clicks."""
    n_samples = int(duration_sec * sample_rate)
    t = np.arange(n_samples, dtype=np.float32) / sample_rate
    wave = np.sin(2 * np.pi * freq_hz * t).astype(np.float32)
    fade = min(n_samples // 4, int(0.005 * sample_rate))
    if fade > 0:
        wave[:fade] *= np.linspace(0.0, 1.0, fade, dtype=np.float32)
        wave[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)
    return wave


class ClickSounds:
    """The sample each click kind plays, at the output's rate.

    Built once per rate rather than per beat: the scheduler asks for a sample
    ~every half second for a whole show, and a rate switch (a device that
    refused 44.1 kHz) is the only thing that invalidates them."""

    def __init__(self, sample_rate: int = SAMPLE_RATE):
        self._samples: Dict[str, np.ndarray] = {}
        self.set_rate(sample_rate)

    def set_rate(self, sample_rate: int) -> None:
        self._samples = {kind: make_beep(hz, sample_rate=sample_rate)
                         for kind, hz in _PITCH_HZ.items()}

    def sample(self, kind: str) -> np.ndarray:
        return self._samples[kind]
