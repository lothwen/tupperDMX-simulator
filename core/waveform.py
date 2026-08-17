# ABOUTME: Amplitude binning for waveform drawing — the Qt-free half shared by
# ABOUTME: the web remote's canvas bins and the desktop timeline's pixmap.
from typing import List, Tuple

import numpy as np


def bin_extremes(samples, bins: int) -> Tuple[np.ndarray, np.ndarray]:
    """Per-bin signed (min, max) over `bins` equal-width slices of `samples`.

    Takes mono samples or (n, channels) frames of any dtype — array_split hands
    out views rather than copies, which matters because a five-minute track is
    ~50 MB of int16 and every intermediate of that size is a real cost on the
    SBC. The extremes come back as floats: abs() wraps a full-scale int16
    -32768 back onto itself, so signedness has to survive the read."""
    samples = np.asarray(samples)
    if bins <= 0:
        return np.zeros(0), np.zeros(0)
    mins = np.zeros(bins)
    maxs = np.zeros(bins)
    if samples.size == 0:
        return mins, maxs
    for i, chunk in enumerate(np.array_split(samples, bins)):
        if chunk.size:
            mins[i] = float(chunk.min())
            maxs[i] = float(chunk.max())
    return mins, maxs


def peak_envelope(samples, bins: int) -> List[float]:
    """Abs-max amplitude per bin, normalized so the loudest bin is 1.0.

    One number per bin — the symmetric envelope a bar chart draws. Callers
    wanting the asymmetric shape (the timeline's pixmap) use bin_extremes."""
    mins, maxs = bin_extremes(samples, bins)
    # One of the two is always >= 0: a bin whose max is negative has a min that
    # is more negative still, so its magnitude wins.
    peaks = np.maximum(maxs, -mins)
    top = peaks.max() if peaks.size else 0.0
    if top > 0:
        peaks = peaks / top
    return [float(p) for p in peaks]
