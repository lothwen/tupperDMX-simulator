# ABOUTME: Waveform peak extraction for the web remote: downsample the PCM the
# ABOUTME: audio engine already decoded into N amplitude bins for canvas rendering.
import os
from typing import Dict, List

from core.waveform import peak_envelope

# (path, mtime, bins) -> peaks; a setlist has a handful of songs, each list is
# ~800 floats, so the cache stays tiny for the app's lifetime.
_peaks_cache: Dict[tuple, List[float]] = {}


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
        _peaks_cache[key] = peak_envelope(pcm, bins)
    return _peaks_cache[key]
