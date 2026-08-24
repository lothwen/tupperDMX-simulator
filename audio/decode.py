# ABOUTME: Async audio-file decoding to raw PCM via QAudioDecoder — the same
# ABOUTME: Qt/ffmpeg backend that used to play these files through QMediaPlayer.

from typing import List, Optional

import numpy as np
from PyQt6.QtCore import QObject, QThread, pyqtSignal, QUrl
from PyQt6.QtMultimedia import QAudioDecoder, QAudioFormat

from audio.mixer import SAMPLE_RATE, resample
from tupperlogger import get_logger


class AudioFileDecoder(QObject):
    """Decodes one file at a time to int16 (n, 2) at the target rate,
    asynchronously.

    A conversion format is requested from the decoder, but each delivered
    buffer's actual format is honoured (mono is duplicated, other sample
    formats are converted, an off-rate result is resampled at the end), so a
    backend that ignores the request still yields correct output."""

    decoded = pyqtSignal(object, str)   # np.ndarray int16 (n, 2), path
    failed = pyqtSignal(str, str)       # error message, path

    def __init__(self, parent=None):
        super().__init__(parent)
        self.logger = get_logger("audio.decode")
        self._decoder = QAudioDecoder(self)
        self._rate = SAMPLE_RATE
        self._request_format()
        self._decoder.bufferReady.connect(self._on_buffer_ready)
        self._decoder.finished.connect(self._on_finished)
        self._decoder.error.connect(self._on_error)
        self._chunks: List[np.ndarray] = []
        self._chunk_rate: Optional[int] = None
        self._path: Optional[str] = None

    def set_target_rate(self, rate: int) -> None:
        """Decode to `rate` from now on — the rate the sink negotiated. A
        decode already in flight lands at the new rate too: the conversion is
        applied when it finishes, not per buffer."""
        if rate == self._rate or rate <= 0:
            return
        self._rate = rate
        self._request_format()

    def _request_format(self) -> None:
        requested = QAudioFormat()
        requested.setSampleRate(self._rate)
        requested.setChannelCount(2)
        requested.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        self._decoder.setAudioFormat(requested)

    def start(self, path: str) -> None:
        """Begin decoding `path`; emits decoded() or failed() when done.
        Starting a new decode cancels the previous one silently."""
        self.cancel()
        self._path = path
        self._decoder.setSource(QUrl.fromLocalFile(path))
        self._decoder.start()

    def cancel(self) -> None:
        """Abandon the decode in flight without reporting anything.

        The state that NAMES the decode goes first, because QAudioDecoder
        emits finished() synchronously from inside stop(): drop it afterwards
        and _on_finished() hands the aborted decode's partial buffers to
        decoded() as if they were the whole file (a 224 s song reported
        'decoded' at 18 s), or — aborted before its first buffer — reports it
        as the failure "Decoded no audio data"."""
        self._path = None
        self._chunks = []
        self._chunk_rate = None
        self._decoder.stop()

    # ------------------------------------------------------------------
    def _on_buffer_ready(self) -> None:
        buf = self._decoder.read()
        if not buf.isValid() or self._path is None:
            return
        fmt = buf.format()
        ptr = buf.constData()
        ptr.setsize(buf.byteCount())
        raw = bytes(ptr)

        sample_format = fmt.sampleFormat()
        if sample_format == QAudioFormat.SampleFormat.Int16:
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        elif sample_format == QAudioFormat.SampleFormat.Float:
            samples = np.frombuffer(raw, dtype=np.float32).copy()
        elif sample_format == QAudioFormat.SampleFormat.Int32:
            samples = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
        elif sample_format == QAudioFormat.SampleFormat.UInt8:
            samples = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        else:
            return

        channels = max(1, fmt.channelCount())
        frames = len(samples) // channels
        samples = samples[: frames * channels].reshape(-1, channels)
        if channels == 1:
            stereo = np.repeat(samples, 2, axis=1)
        else:
            stereo = samples[:, :2]

        self._chunk_rate = fmt.sampleRate()
        self._chunks.append(stereo)

    def _on_finished(self) -> None:
        path = self._path
        if path is None:
            return
        self._path = None
        if not self._chunks:
            self.failed.emit("Decoded no audio data", path)
            return

        data = np.concatenate(self._chunks)
        self._chunks = []

        data = resample(data, self._chunk_rate or self._rate, self._rate)

        pcm = np.clip(data * 32767.0, -32768, 32767).astype(np.int16)
        self.logger.info(
            f"Decoded {path}: {len(pcm) / self._rate:.1f}s @ {self._rate}Hz")
        self.decoded.emit(pcm, path)

    def _on_error(self, _error) -> None:
        path = self._path
        if path is None:
            return
        self._path = None
        self._chunks = []
        message = self._decoder.errorString() or "Audio decode error"
        self.logger.error(f"Decode failed for {path}: {message}")
        self.failed.emit(message, path)


class _DecodeWorker(QObject):
    """The half of ThreadedAudioDecoder that lives on the decode thread.

    It BUILDS the AudioFileDecoder rather than receiving one: a QAudioDecoder
    constructed on the Qt thread and moveToThread()'d afterwards never decodes
    a byte on the ffmpeg backend."""

    decoded = pyqtSignal(object, str)
    failed = pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self._decoder: Optional[AudioFileDecoder] = None

    def build(self) -> None:
        """Runs on the decode thread (QThread.started), before its event loop
        processes anything this object was sent — so every start/cancel below
        arrives with the decoder already in place."""
        self._decoder = AudioFileDecoder(self)
        self._decoder.decoded.connect(self.decoded)
        self._decoder.failed.connect(self.failed)

    def start(self, path: str) -> None:
        self._decoder.start(path)

    def cancel(self) -> None:
        self._decoder.cancel()

    def set_target_rate(self, rate: int) -> None:
        self._decoder.set_target_rate(rate)


class ThreadedAudioDecoder(QObject):
    """AudioFileDecoder on a thread of its own — same interface, off the GUI.

    Decoding costs ONE bufferReady slot call per buffer (~9000 for a
    four-minute song), so on the Qt thread its speed is whatever the stage
    renderer leaves of the event loop: a track that decodes in 0.15 s idle
    takes over 4 s behind the GPU fog march. That matters because every song
    switch and every monitor toggle RESTARTS the decode from zero — a few
    toggles inside a multi-second window and the track simply never loads,
    with `_loading_path` pinned and nothing to show for it.

    Requests cross as queued signals to ONE receiver, so their order is the
    order they were made in; results cross back the same way."""

    decoded = pyqtSignal(object, str)   # np.ndarray int16 (n, 2), path
    failed = pyqtSignal(str, str)       # error message, path

    _start_requested = pyqtSignal(str)
    _cancel_requested = pyqtSignal()
    _rate_requested = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = QThread()
        self._thread.setObjectName("audio-decode")
        self._worker = _DecodeWorker()
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.build)
        self._start_requested.connect(self._worker.start)
        self._cancel_requested.connect(self._worker.cancel)
        self._rate_requested.connect(self._worker.set_target_rate)
        # Explicit slots rather than signal-to-signal: this is the boundary
        # the results come back across, and it must be a queued hop onto the
        # owner's thread, never a direct call from the decode thread.
        self._worker.decoded.connect(self._on_decoded)
        self._worker.failed.connect(self._on_failed)

        self._thread.start()

    def start(self, path: str) -> None:
        self._start_requested.emit(path)

    def cancel(self) -> None:
        self._cancel_requested.emit()

    def set_target_rate(self, rate: int) -> None:
        self._rate_requested.emit(rate)

    def shutdown(self) -> None:
        """Stop decoding and join the thread (idempotent)."""
        if not self._thread.isRunning():
            return
        self._cancel_requested.emit()
        self._thread.quit()
        self._thread.wait(3000)

    def _on_decoded(self, pcm, path: str) -> None:
        self.decoded.emit(pcm, path)

    def _on_failed(self, message: str, path: str) -> None:
        self.failed.emit(message, path)
