"""
Regression test: the file decoder's two failure modes behind "no track loads".

1. An ABORTED decode must report nothing. QAudioDecoder emits finished()
   synchronously from inside stop(), so a start() that stops the previous
   decode before dropping the state naming it hands the partial buffers to
   decoded() as a complete file (a 224 s song "decoded" at 18 s in a real
   session log), or reports "Decoded no audio data" for an abort that had not
   produced a buffer yet.

2. Decoding must not ride the GUI event loop. It costs one bufferReady slot
   call per buffer — thousands per song — so on the Qt thread its speed is
   whatever the stage renderer leaves free. With the GPU fog march running,
   a decode that takes 0.15 s idle takes over 4 s; since every song switch and
   monitor toggle restarts it from zero, a couple of toggles inside that window
   leave the engine with `_loading_path` pinned and no track loaded at all.

Run with:
    uv run python tests/test_audio_decode.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import threading
import time
import wave

import numpy as np
from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication

from audio.decode import AudioFileDecoder, ThreadedAudioDecoder

# Long enough that decoding is thousands of buffers (which is the whole point
# of test 2), short enough to generate and decode in a moment.
_SECONDS = 60
_RATE = 44100


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _write_wav(path):
    """A stereo 44.1 kHz tone of _SECONDS — the reference the decoder should
    hand back whole."""
    t = np.arange(_SECONDS * _RATE, dtype=np.float32) / _RATE
    mono = (0.2 * np.sin(2 * np.pi * 440.0 * t) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(_RATE)
        w.writeframes(np.repeat(mono[:, None], 2, axis=1).tobytes())
    return path


def _pump(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class _Hog:
    """Saturates the Qt event loop the way the GPU fog march does."""

    def __init__(self, busy_ms=28, period_ms=30):
        self._timer = QTimer()
        self._timer.setInterval(period_ms)
        self._timer.timeout.connect(lambda: time.sleep(busy_ms / 1000.0))

    def __enter__(self):
        self._timer.start()
        return self

    def __exit__(self, *exc):
        self._timer.stop()


def _decode(decoder, path, timeout_s=30.0):
    """Run one decode to completion; returns (seconds_of_audio, wall_time)."""
    result = []
    decoder.decoded.connect(lambda pcm, p: result.append((len(pcm) / _RATE, p)))
    decoder.failed.connect(lambda m, p: result.append((m, p)))
    start = time.time()
    decoder.start(path)
    while not result and time.time() - start < timeout_s:
        _pump(10)
    elapsed = time.time() - start
    decoder.decoded.disconnect()
    decoder.failed.disconnect()
    return (result[0] if result else None), elapsed


# ----------------------------------------------------------------------
# 1. An aborted decode reports nothing
# ----------------------------------------------------------------------
def test_aborted_decode_reports_nothing():
    print("test_aborted_decode_reports_nothing")
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_wav(os.path.join(tmp, "tone.wav"))
        other = _write_wav(os.path.join(tmp, "other.wav"))

        decoder = AudioFileDecoder()
        reported = []
        decoder.decoded.connect(
            lambda pcm, p: reported.append(("decoded", p, len(pcm) / _RATE)))
        decoder.failed.connect(lambda m, p: reported.append(("failed", p, m)))

        # Aborted before its first buffer: not a failure, just abandoned.
        decoder.start(path)
        _assert(decoder._decoder.isDecoding(), "the decode really is in flight")
        decoder.start(other)
        _assert(reported == [],
                "an abort before the first buffer is not a decode failure")

        # Aborted with buffers collected: those buffers are NOT the file, and
        # handing them to decoded() is how a 224 s song came back as 18 s.
        decoder.cancel()
        decoder.start(path)
        for _ in range(50):
            _pump(1)
            if decoder._chunks:
                break
        _assert(decoder._chunks and not reported,
                "the decode is mid-flight, with buffers already collected")
        decoder.start(other)
        _assert(reported == [],
                "restarting mid-decode reports neither decoded nor failed")
        _assert(decoder._chunks == [],
                "...and the aborted decode's buffers cannot leak into the next")

        # The decoder still works afterwards.
        decoder.cancel()
        got, _ = _decode(decoder, path)
        _assert(got is not None and got[1] == path,
                "the decoder still decodes after an abort")
        _assert(abs(got[0] - _SECONDS) < 0.5,
                f"...and reports the whole file ({got[0]:.1f}s)")


# ----------------------------------------------------------------------
# 2. Decoding does not ride the GUI event loop
# ----------------------------------------------------------------------
def test_decode_runs_off_the_qt_thread():
    print("test_decode_runs_off_the_qt_thread")
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_wav(os.path.join(tmp, "tone.wav"))
        decoder = ThreadedAudioDecoder()
        try:
            seen = []
            decoder.decoded.connect(
                lambda pcm, p: seen.append(threading.current_thread()))
            got, _ = _decode(decoder, path)
            _assert(got is not None and abs(got[0] - _SECONDS) < 0.5,
                    "the threaded decoder returns the whole file")
            _assert(seen and seen[0] is threading.main_thread(),
                    "the result crosses back onto the owner's thread")
            _assert(decoder._worker._decoder.thread() is not decoder.thread(),
                    "the QAudioDecoder itself lives off the owner's thread")
        finally:
            decoder.shutdown()


def test_decode_time_is_independent_of_gui_load():
    print("test_decode_time_is_independent_of_gui_load")
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_wav(os.path.join(tmp, "tone.wav"))
        decoder = ThreadedAudioDecoder()
        try:
            _, idle = _decode(decoder, path)
            with _Hog():
                _, busy = _decode(decoder, path)
            # On the Qt thread this ratio was ~30x, which is what turned a
            # song switch into a track that never finished loading.
            budget = max(0.5, 5 * idle)
            _assert(busy < budget,
                    f"decode under a saturated event loop stays fast "
                    f"({busy:.2f}s vs {idle:.2f}s idle, budget {budget:.2f}s)")
        finally:
            decoder.shutdown()


def test_rapid_restarts_converge_on_the_last_request():
    print("test_rapid_restarts_converge_on_the_last_request")
    with tempfile.TemporaryDirectory() as tmp:
        first = _write_wav(os.path.join(tmp, "first.wav"))
        last = _write_wav(os.path.join(tmp, "last.wav"))
        decoder = ThreadedAudioDecoder()
        try:
            reported = []
            decoder.decoded.connect(
                lambda pcm, p: reported.append((os.path.basename(p),
                                                len(pcm) / _RATE)))
            decoder.failed.connect(
                lambda m, p: reported.append((os.path.basename(p), m)))

            # A monitor toggled back and forth under load, the way the bug is
            # triggered: whatever comes back must be a WHOLE file, never an
            # aborted decode's leftovers, and the last request must win.
            with _Hog():
                for _ in range(8):
                    decoder.start(first)
                    _pump(20)
                    decoder.start(last)
                    _pump(20)
                deadline = time.time() + 30.0
                while not reported and time.time() < deadline:
                    _pump(10)
                _pump(500)

            _assert(all(isinstance(length, float) for _, length in reported),
                    f"no decode is reported as failed (got {reported})")
            _assert(all(abs(length - _SECONDS) < 0.5 for _, length in reported),
                    f"no decode is reported truncated (got {reported})")
            _assert(reported and reported[-1][0] == "last.wav",
                    "the last request is what ends up decoded")
        finally:
            decoder.shutdown()


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    test_aborted_decode_reports_nothing()
    test_decode_runs_off_the_qt_thread()
    test_decode_time_is_independent_of_gui_load()
    test_rapid_restarts_converge_on_the_last_request()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
