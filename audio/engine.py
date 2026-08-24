# ABOUTME: AudioEngine — the Qt bridge that streams the Mixer through ONE stereo
# ABOUTME: QAudioSink (pull mode) on a selectable device; buses: backing/click/voice.

from typing import Dict, Optional

import numpy as np
from PyQt6.QtCore import QIODevice, QObject, QTimer, pyqtSignal
from PyQt6.QtMultimedia import (QAudio, QAudioDevice, QAudioFormat, QAudioSink,
                                QMediaDevices)

from audio.decode import ThreadedAudioDecoder
from audio.mixer import (BUS_CLICK, BUS_VOICE, METRONOME_HIGH_PITCH_HZ,
                         METRONOME_LOW_PITCH_HZ, SAMPLE_RATE, Mixer, make_beep,
                         resample)
from tupperlogger import get_logger

# Target sink buffer: latency shared equally by backing/click/voice, so their
# mutual sync is exact; the absolute skew vs the lights is absorbed by the
# per-song audio offset (same as QMediaPlayer's own pipeline latency before).
_BUFFER_SECONDS = 0.12
# Push-feed cadence: several top-ups fit inside the buffer, so occasional
# event-loop hiccups don't underrun.
_FEED_INTERVAL_MS = 15

# Rates to offer a device, after the reference rate and the device's own
# preferred one: the rates a sound card that refuses both is likely to take.
_CANDIDATE_RATES = (48000, 44100, 96000, 88200, 32000, 22050, 16000)
# Float first (the mixer already renders float), Int16 as the universal one.
_SAMPLE_FORMATS = (QAudioFormat.SampleFormat.Float,
                   QAudioFormat.SampleFormat.Int16)


def stereo_format(rate: int, sample_format) -> QAudioFormat:
    fmt = QAudioFormat()
    fmt.setSampleRate(rate)
    fmt.setChannelCount(2)
    fmt.setSampleFormat(sample_format)
    return fmt


def candidate_formats(device: QAudioDevice) -> list:
    """Stereo formats to try on `device`, best first.

    Rate order is the reference rate (source files and the TTS cache are
    44.1 kHz, so matching it resamples nothing), then whatever the device says
    it prefers, then common ones — every combination the device confirms,
    followed by its preferred rate UNCONFIRMED. That tail matters: a device
    reports one rate range and shared-mode WASAPI reports exactly its 48 kHz
    mix format, so on Windows every 44.1 kHz format is refused; but a sink that
    starts and plays beats disabling audio on the device's say-so."""
    preferred = device.preferredFormat().sampleRate()
    rates = []
    for rate in (SAMPLE_RATE, preferred, *_CANDIDATE_RATES):
        if rate > 0 and rate not in rates:
            rates.append(rate)

    formats, seen = [], set()
    for rate in rates:
        for sample_format in _SAMPLE_FORMATS:
            if device.isFormatSupported(stereo_format(rate, sample_format)):
                formats.append(stereo_format(rate, sample_format))
                seen.add((rate, sample_format))
    for sample_format in _SAMPLE_FORMATS:
        key = (preferred or SAMPLE_RATE, sample_format)
        if key not in seen:
            formats.append(stereo_format(*key))
    return formats


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

        self._decoder = ThreadedAudioDecoder(self)
        self._decoder.decoded.connect(self._on_decoded)
        self._decoder.failed.connect(self._on_decode_failed)

        self._sink: Optional[QAudioSink] = None
        self._sink_device: Optional[QIODevice] = None  # push-mode target
        self._rate = SAMPLE_RATE   # the whole pipeline's rate; the sink picks it
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
        """Open the sink on the configured device with the first format that
        device actually accepts, and retune the pipeline to its rate."""
        self._stop_sink()
        try:
            device = self._resolve_device()
            if device.isNull():
                self.logger.warning("No audio output device available")
                return

            for fmt in candidate_formats(device):
                sink, sink_device = self._open_sink(device, fmt)
                if sink is None:
                    continue
                self._sink = sink
                self._sink_device = sink_device
                self._use_float = (
                    fmt.sampleFormat() == QAudioFormat.SampleFormat.Float)
                self._bytes_per_frame = 8 if self._use_float else 4
                self._apply_rate(fmt.sampleRate())
                self._feed_timer.start()
                self._on_feed()  # prime the buffer immediately
                self.logger.info(
                    f"Audio sink started on '{device.description()}' "
                    f"({fmt.sampleRate()} Hz, "
                    f"{'float32' if self._use_float else 'int16'}, "
                    f"buffer {sink.bufferSize()} bytes)")
                return

            self.logger.error(
                f"Device '{device.description()}' started at no stereo format "
                f"(it reports {device.minimumSampleRate()}-"
                f"{device.maximumSampleRate()} Hz); audio disabled")
        except Exception as e:
            self.logger.error(f"Failed to start audio sink: {e}")
            self._stop_sink()

    def _open_sink(self, device: QAudioDevice, fmt: QAudioFormat):
        """Start one QAudioSink, or (None, None). Tried rather than trusted:
        isFormatSupported() both over- and under-reports depending on the
        backend, so only start() settles it."""
        bytes_per_frame = (
            8 if fmt.sampleFormat() == QAudioFormat.SampleFormat.Float else 4)
        try:
            sink = QAudioSink(device, fmt, self)
            sink.setBufferSize(
                int(_BUFFER_SECONDS * fmt.sampleRate() * bytes_per_frame))
            # Push mode: Qt owns the device; _on_feed writes into it.
            sink_device = sink.start()
            if sink_device is not None and sink.error() != QAudio.Error.OpenError:
                return sink, sink_device
            self.logger.warning(
                f"'{device.description()}' refused {fmt.sampleRate()} Hz "
                f"{'float32' if bytes_per_frame == 8 else 'int16'} stereo")
            sink.stop()
            sink.deleteLater()
        except Exception as e:
            self.logger.warning(
                f"Failed to open audio sink at {fmt.sampleRate()} Hz: {e}")
        return None, None

    def _apply_rate(self, rate: int) -> None:
        """Retune the whole mixing chain to the sink's rate.

        Everything loaded is at the old rate, so the clicks are regenerated,
        the decoder is re-targeted and a loaded song is resampled where it
        stands, keeping its position — the transport asks the mixer for
        seconds, so a rate switch mid-song stays inaudible beyond the sink
        restart. Called with the sink stopped."""
        if rate == self._rate or rate <= 0:
            return
        previous, self._rate = self._rate, rate
        self._click_high = make_beep(METRONOME_HIGH_PITCH_HZ, sample_rate=rate)
        self._click_low = make_beep(METRONOME_LOW_PITCH_HZ, sample_rate=rate)
        self._decoder.set_target_rate(rate)

        pcm = self.mixer.backing_samples()
        position = self.mixer.position_seconds()
        self.mixer.clear_oneshots()   # queued at the old rate
        self.mixer.set_sample_rate(rate)
        if pcm is not None:
            self.mixer.set_backing(resample(pcm, previous, rate))
            self.mixer.set_position_seconds(position)
        self.logger.info(f"Audio pipeline retuned {previous} Hz -> {rate} Hz")

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
        if path == self._loading_path:
            return  # same decode already in flight; restarting it can abort it
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

    def backing_samples(self):
        """The decoded PCM of the loaded track (int16 (n, 2)), or None."""
        return self.mixer.backing_samples()

    def loaded_path(self) -> Optional[str]:
        """The file that PCM came from — the guide when it is the monitored
        track, so never assume it is the song's backing."""
        return self._loaded_path

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
        """Queue a mono float32 clip on the voice bus (TTS cues, spoken count).

        Clips arrive at the TTS reference rate; conforming them to the sink's
        rate here keeps tts.py's cache canonical at 44.1 kHz. A cue is a second
        of mono — orders of magnitude below the synthesis that never runs
        during playback."""
        self.mixer.trigger(BUS_VOICE, resample(samples, SAMPLE_RATE, self._rate))

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
        self._decoder.shutdown()
        self._stop_sink()
