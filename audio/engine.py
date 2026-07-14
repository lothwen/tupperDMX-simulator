# ABOUTME: AudioEngine — the Qt bridge that streams the Mixer through ONE stereo
# ABOUTME: QAudioSink (pull mode) on a selectable device; buses: backing/click/voice.

from typing import Dict, Optional

import numpy as np
from PyQt6.QtCore import QIODevice, QObject, QTimer, pyqtSignal
from PyQt6.QtMultimedia import (QAudioDevice, QAudioFormat, QAudioSink,
                                QMediaDevices)

from audio.decode import AudioFileDecoder
from audio.mixer import (BUS_CLICK, BUS_VOICE, METRONOME_HIGH_PITCH_HZ,
                         METRONOME_LOW_PITCH_HZ, SAMPLE_RATE, Mixer, make_beep)
from tupperlogger import get_logger

# Target sink buffer: latency shared equally by backing/click/voice, so their
# mutual sync is exact; the absolute skew vs the lights is absorbed by the
# per-song audio offset (same as QMediaPlayer's own pipeline latency before).
_BUFFER_SECONDS = 0.12
# Push-feed cadence: several top-ups fit inside the buffer, so occasional
# event-loop hiccups don't underrun.
_FEED_INTERVAL_MS = 15


def device_id_str(device: QAudioDevice) -> str:
    """Stable string form of a QAudioDevice id (config round-trippable)."""
    return bytes(device.id()).decode("utf-8", errors="replace")


class AudioEngine(QObject):
    """Single audio output for the whole app. Owns the Mixer (backing / click /
    voice buses), a push-mode QAudioSink on the configured output device and
    the async file decoder. The sink runs continuously so click beeps and voice
    previews work while transport is stopped.

    Push mode on purpose: a Qt-thread timer tops up the sink's own device, so
    NO Python code ever runs on the audio callback thread — a pull-mode
    QIODevice implemented in Python deadlocks/aborts inside QAudioSink.stop()
    (the audio thread blocks on the GIL while stop() waits for it)."""

    backing_loaded = pyqtSignal(str)         # path
    backing_failed = pyqtSignal(str, str)    # error message, path
    finished = pyqtSignal()                  # backing ran past its end while playing

    def __init__(self, parent=None):
        super().__init__(parent)
        self.logger = get_logger("audio.engine")
        self.mixer = Mixer()

        self._decoder = AudioFileDecoder(self)
        self._decoder.decoded.connect(self._on_decoded)
        self._decoder.failed.connect(self._on_decode_failed)

        self._sink: Optional[QAudioSink] = None
        self._sink_device: Optional[QIODevice] = None  # push-mode target
        self._use_float = True
        self._bytes_per_frame = 8
        self._device_id = ""
        self._loaded_path: Optional[str] = None
        self._loading_path: Optional[str] = None

        # Push feed: keeps the sink buffer topped up from the Qt thread.
        self._feed_timer = QTimer(self)
        self._feed_timer.setInterval(_FEED_INTERVAL_MS)
        self._feed_timer.timeout.connect(self._on_feed)

        self._click_high = make_beep(METRONOME_HIGH_PITCH_HZ)
        self._click_low = make_beep(METRONOME_LOW_PITCH_HZ)

        # End-of-backing watchdog: polls the mixer latch from the Qt thread
        # (render() runs on the audio thread and must not emit signals).
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(100)
        self._watchdog.timeout.connect(self._on_watchdog)
        self._watchdog.start()

        self._start_sink()

    # ------------------------------------------------------------------
    # Output device / routing
    # ------------------------------------------------------------------
    def set_output_device(self, device_id: str) -> None:
        """Select the output device by id string ('' = system default)."""
        if device_id == self._device_id and self._sink is not None:
            return
        self._device_id = device_id or ""
        self._start_sink()

    def set_routing(self, routing: str) -> None:
        self.mixer.set_routing(routing)

    def _resolve_device(self) -> QAudioDevice:
        if self._device_id:
            for device in QMediaDevices.audioOutputs():
                if device_id_str(device) == self._device_id:
                    return device
            self.logger.warning(
                f"Audio output '{self._device_id}' not found, using default")
        return QMediaDevices.defaultAudioOutput()

    def _start_sink(self) -> None:
        self._stop_sink()
        try:
            device = self._resolve_device()
            if device.isNull():
                self.logger.warning("No audio output device available")
                return

            fmt = QAudioFormat()
            fmt.setSampleRate(SAMPLE_RATE)
            fmt.setChannelCount(2)
            fmt.setSampleFormat(QAudioFormat.SampleFormat.Float)
            use_float = device.isFormatSupported(fmt)
            if not use_float:
                fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
                if not device.isFormatSupported(fmt):
                    self.logger.warning(
                        f"Device '{device.description()}' supports neither "
                        "Float nor Int16 stereo @ 44100; audio disabled")
                    return

            self._use_float = use_float
            self._bytes_per_frame = 8 if use_float else 4
            self._sink = QAudioSink(device, fmt, self)
            self._sink.setBufferSize(
                int(_BUFFER_SECONDS * SAMPLE_RATE * self._bytes_per_frame))
            # Push mode: Qt owns the device; _on_feed writes into it.
            self._sink_device = self._sink.start()
            if self._sink_device is None:
                self.logger.error("Audio sink returned no device; audio disabled")
                self._sink = None
                return
            self._feed_timer.start()
            self._on_feed()  # prime the buffer immediately
            self.logger.info(
                f"Audio sink started on '{device.description()}' "
                f"({'float32' if use_float else 'int16'}, "
                f"buffer {self._sink.bufferSize()} bytes)")
        except Exception as e:
            self.logger.error(f"Failed to start audio sink: {e}")
            self._sink = None
            self._sink_device = None

    def _stop_sink(self) -> None:
        self._feed_timer.stop()
        self._sink_device = None
        if self._sink is not None:
            try:
                self._sink.stop()
            except Exception as e:
                self.logger.warning(f"Error stopping audio sink: {e}")
            self._sink = None

    def _on_feed(self) -> None:
        """Top up the sink buffer with freshly mixed audio (Qt thread)."""
        if self._sink is None or self._sink_device is None:
            return
        frames = self._sink.bytesFree() // self._bytes_per_frame
        if frames <= 0:
            return
        block = self.mixer.render(frames)
        if self._use_float:
            payload = block.astype(np.float32).tobytes()
        else:
            payload = (block * 32767.0).astype(np.int16).tobytes()
        self._sink_device.write(payload)

    # ------------------------------------------------------------------
    # Backing track
    # ------------------------------------------------------------------
    def load_backing(self, path: str) -> None:
        """Decode `path` asynchronously; emits backing_loaded/backing_failed."""
        if path == self._loaded_path and self.mixer.has_backing():
            self.backing_loaded.emit(path)
            return
        self._loading_path = path
        self.mixer.set_backing(None)
        self._loaded_path = None
        self._decoder.start(path)

    def unload_backing(self) -> None:
        self._decoder.cancel()
        self._loading_path = None
        self._loaded_path = None
        self.mixer.set_backing(None)

    def is_backing_loaded(self) -> bool:
        return self.mixer.has_backing()

    def duration(self) -> float:
        return self.mixer.duration_seconds()

    def play(self) -> None:
        self.mixer.set_backing_active(True)

    def pause(self) -> None:
        self.mixer.set_backing_active(False)

    def is_active(self) -> bool:
        return self.mixer.backing_active()

    def set_backing_position(self, seconds: float) -> None:
        self.mixer.set_position_seconds(seconds)

    def backing_position(self) -> float:
        return self.mixer.position_seconds()

    # ------------------------------------------------------------------
    # Click / voice buses
    # ------------------------------------------------------------------
    def trigger_click(self, downbeat: bool) -> None:
        self.mixer.trigger(BUS_CLICK, self._click_high if downbeat else self._click_low)

    def play_voice(self, samples: np.ndarray) -> None:
        """Queue a mono float32 clip on the voice bus (TTS cues, spoken count)."""
        self.mixer.trigger(BUS_VOICE, samples)

    def stop_voices(self) -> None:
        self.mixer.clear_oneshots(BUS_VOICE)

    def set_bus_volume(self, bus: str, volume: float) -> None:
        self.mixer.set_volume(bus, volume)

    def set_bus_muted(self, bus: str, muted: bool) -> None:
        self.mixer.set_muted(bus, muted)

    def levels(self) -> Dict[str, float]:
        return self.mixer.levels()

    # ------------------------------------------------------------------
    def _on_decoded(self, pcm, path: str) -> None:
        if path != self._loading_path:
            return  # a newer load superseded this decode
        self._loading_path = None
        self._loaded_path = path
        self.mixer.set_backing(pcm)
        self.backing_loaded.emit(path)

    def _on_decode_failed(self, message: str, path: str) -> None:
        if path != self._loading_path:
            return
        self._loading_path = None
        self.backing_failed.emit(message, path)

    def _on_watchdog(self) -> None:
        if self.mixer.consume_backing_ended():
            self.finished.emit()

    def cleanup(self) -> None:
        self.logger.info("Cleaning up audio engine")
        self._watchdog.stop()
        self._decoder.cancel()
        self._stop_sink()
