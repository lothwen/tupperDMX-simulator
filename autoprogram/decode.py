# ABOUTME: Blocking decode of an audio file to the mono signal analysis.py
# ABOUTME: reads, through the app's own AudioFileDecoder. QtCore.
"""A song's samples as the analysis wants them: the app's decoder, so these
are the samples the app plays, folded to mono and normalised to a peak of 1.

Blocking: it runs an event loop of its own until the decoder answers, so it
belongs on a thread that may wait — a script, a test, a worker thread.
"""
from typing import Tuple

import numpy as np
from PyQt6.QtCore import QCoreApplication, QEventLoop

from audio.decode import AudioFileDecoder
from audio.mixer import SAMPLE_RATE

_app = None


def mono(pcm: np.ndarray) -> np.ndarray:
    """int16 frames (n, 2) as one float32 channel with a peak of 1."""
    out = pcm.astype(np.float32).mean(axis=1) / 32768.0
    peak = float(np.abs(out).max()) if len(out) else 0.0
    if peak > 0:
        out /= peak
    return out


def decode(path: str) -> Tuple[int, np.ndarray]:
    """``(rate, mono)`` of the audio file at `path`."""
    global _app                       # kept: an unreferenced app is collected at once
    _app = QCoreApplication.instance() or QCoreApplication([])
    decoder = AudioFileDecoder()
    loop = QEventLoop()
    result = {}

    def done(key, value):
        result[key] = value
        loop.quit()

    decoder.decoded.connect(lambda pcm, _path: done("pcm", pcm))
    decoder.failed.connect(lambda message, _path: done("error", message))
    decoder.start(path)
    if not result:
        loop.exec()
    if "error" in result:
        raise RuntimeError(f"cannot decode {path}: {result['error']}")
    return SAMPLE_RATE, mono(result["pcm"])
