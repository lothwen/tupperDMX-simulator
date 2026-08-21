# ABOUTME: Async audio-file decoding to raw PCM via QAudioDecoder — the same
# ABOUTME: Qt/ffmpeg backend that used to play these files through QMediaPlayer.

from typing import List, Optional

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal, QUrl
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
        self._decoder.stop()
        self._chunks = []
        self._chunk_rate = None
        self._path = path
        self._decoder.setSource(QUrl.fromLocalFile(path))
        self._decoder.start()

    def cancel(self) -> None:
        self._path = None
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
