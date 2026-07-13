# ABOUTME: Waveform peak extraction for the web remote: load audio at low
# ABOUTME: sample rate and downsample to N amplitude bins for canvas rendering.
import os
from typing import Dict, List

import numpy as np

# (path, mtime, bins) -> peaks; a setlist has a handful of songs, each list is
# ~800 floats, so the cache stays tiny for the app's lifetime.
_peaks_cache: Dict[tuple, List[float]] = {}


def _downsample(samples, bins: int) -> List[float]:
    """Abs-max amplitude per equal-width bin, normalized to peak 1.0."""
    samples = np.asarray(samples)
    if samples.size == 0:
        return [0.0] * bins
    chunks = np.array_split(np.abs(samples), bins)
    peaks = np.array([chunk.max() if chunk.size else 0.0 for chunk in chunks])
    top = peaks.max()
    if top > 0:
        peaks = peaks / top
    return [float(p) for p in peaks]


def compute_peaks(audio_path: str, bins: int = 800) -> List[float]:
    """Downsampled waveform peaks for one audio file, cached by mtime.
    Loading at sr=8000 mono keeps this a fraction of a full-quality load;
    peak shape is indistinguishable at ~800 screen bins."""
    mtime = os.path.getmtime(audio_path)
    key = (audio_path, mtime, bins)
    if key not in _peaks_cache:
        import librosa  # deferred: heavy import, only needed on first request
        samples, _sr = librosa.load(audio_path, sr=8000, mono=True)
        _peaks_cache[key] = _downsample(samples, bins)
    return _peaks_cache[key]
