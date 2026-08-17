# ABOUTME: Waveform peak extraction for the web remote: downsample the PCM the
# ABOUTME: audio engine already decoded into N amplitude bins for canvas rendering.
import os
from typing import Dict, List

import numpy as np

# (path, mtime, bins) -> peaks; a setlist has a handful of songs, each list is
# ~800 floats, so the cache stays tiny for the app's lifetime.
_peaks_cache: Dict[tuple, List[float]] = {}


def _downsample(samples, bins: int) -> List[float]:
    """Abs-max amplitude per equal-width bin, normalized to peak 1.0.

    Takes mono samples or (n, channels) frames of any dtype: array_split hands
    out views rather than copies (a 5-minute track is ~50 MB of int16, and an
    abs() of the whole thing would be another 50 MB), and the peak is read off
    min/max in float because abs() wraps a full-scale int16 -32768 back onto
    itself."""
    samples = np.asarray(samples)
    if samples.size == 0:
        return [0.0] * bins
    chunks = np.array_split(samples, bins)
    peaks = np.array([max(float(c.max()), -float(c.min())) if c.size else 0.0
                      for c in chunks])
    top = peaks.max()
    if top > 0:
        peaks = peaks / top
    return [float(p) for p in peaks]


def compute_peaks(decoded_path: str, pcm, bins: int = 800) -> List[float]:
    """Downsampled waveform peaks for already-decoded audio, cached by mtime.

    `pcm` is what the engine holds for the track it is monitoring — int16
    (n, 2) at the mixer's rate — so nothing is opened, decoded or resampled
    here. `decoded_path` is the cache identity only, and it is the path the
    engine actually decoded rather than the song's: a song's guide and its
    backing are different audio and must not share an entry."""
    try:
        mtime = os.path.getmtime(decoded_path)
    except OSError:
        mtime = 0.0
    key = (decoded_path, mtime, bins)
    if key not in _peaks_cache:
        _peaks_cache[key] = _downsample(pcm, bins)
    return _peaks_cache[key]
