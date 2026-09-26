# ABOUTME: AudioEngine — the Qt bridge that streams the Mixer through ONE stereo
# ABOUTME: QAudioSink (pull mode) on a selectable device; buses: backing/click/voice.

from typing import Dict, Optional

import numpy as np
from PyQt6.QtCore import (QIODevice, QMetaObject, QObject, Qt, QThread,
                          QTimer, pyqtSignal, pyqtSlot)
from PyQt6.QtMultimedia import (QAudio, QAudioDevice, QAudioFormat, QAudioSink,
                                QMediaDevices)

from audio.decode import ThreadedAudioDecoder
from audio.diagnostics import TimingMonitor, maybe_start_attributor
from audio.click import ClickSounds
from audio.mixer import BUS_CLICK, BUS_VOICE, SAMPLE_RATE, Mixer, resample
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


class _FeedWorker(QObject):
    """Owns the QAudioSink and the feed loop, on the AUDIO thread.

    This exists because the stage can block the Qt thread. Measured on a real
    rig: 46-129 ms at a stretch, inside Qt/GL C++ with no Python frame above
    `app.exec()` — long enough to empty an 80 ms buffer, and it produced a
    dropout roughly every 1.2 s of playback. A feed that shares that thread
    cannot win; one that does not is untouched, because the code doing the
    blocking is C++ and holds no GIL while it runs.

    Open and close are BLOCKING queued calls from the Qt thread, so the old
    synchronous ordering survives intact: the sink is up before the pipeline
    retunes to its rate, and feeding starts only afterwards. Only the periodic
    top-up is concurrent, which is the whole point.

    The sink is created, written, read and stopped here and nowhere else —
    QAudioSink has thread affinity, and split ownership is how it crashes."""

    def __init__(self, mixer: Mixer, monitor: TimingMonitor, logger):
        super().__init__()          # no parent: it is moved to another thread
        self.mixer = mixer
        self.monitor = monitor
        self.logger = logger
        self.device_id = ""         # written by the Qt thread before an open
        self.rate = SAMPLE_RATE
        self.use_float = True
        self.bytes_per_frame = 8
        self.buffer_bytes = 0
        self.opened = False
        self._sink: Optional[QAudioSink] = None
        self._sink_device: Optional[QIODevice] = None
        self._timer: Optional[QTimer] = None

    # -- called on the audio thread (blocking queued from the Qt thread) --
    @pyqtSlot()
    def open_sink(self) -> None:
        """Open the configured device at the first format it really accepts."""
        self.close_sink()
        self.opened = False
        try:
            device = self._resolve_device()
            if device.isNull():
                self.logger.warning("No audio output device available")
                return
            for fmt in candidate_formats(device):
                sink, sink_device = self._try_format(device, fmt)
                if sink is None:
                    continue
                self._sink, self._sink_device = sink, sink_device
                self.use_float = (
                    fmt.sampleFormat() == QAudioFormat.SampleFormat.Float)
                self.bytes_per_frame = 8 if self.use_float else 4
                self.rate = fmt.sampleRate()
                self.buffer_bytes = sink.bufferSize()
                self.opened = True
                buffer_ms = (self.buffer_bytes / self.bytes_per_frame
                             / max(1, self.rate) * 1000)
                self.logger.info(
                    f"Audio sink started on '{device.description()}' "
                    f"({self.rate} Hz, "
                    f"{'float32' if self.use_float else 'int16'}, "
                    f"buffer {buffer_ms:.0f} ms / {self.buffer_bytes} bytes)")
                # A backend may grant less than asked (Qt/PulseAudio commonly
                # does). The buffer IS the headroom against a stall, so a short
                # one is worth saying out loud rather than leaving the 120 ms
                # in the source to be believed.
                if buffer_ms < _BUFFER_SECONDS * 1000 * 0.9:
                    self.logger.warning(
                        f"Audio buffer is {buffer_ms:.0f} ms of the "
                        f"{_BUFFER_SECONDS * 1000:.0f} ms requested — less "
                        "headroom before a stall becomes a dropout")
                return
            self.logger.error(
                f"Device '{device.description()}' started at no stereo format "
                f"(it reports {device.minimumSampleRate()}-"
                f"{device.maximumSampleRate()} Hz); audio disabled")
        except Exception as e:
            self.logger.error(f"Failed to start audio sink: {e}")
            self.close_sink()

    @pyqtSlot()
    def begin_feeding(self) -> None:
        """Prime the buffer and start the top-up timer (audio thread)."""
        if not self.opened:
            return
        if self._timer is None:
            self._timer = QTimer(self)
            self._timer.setInterval(_FEED_INTERVAL_MS)
            self._timer.setTimerType(Qt.TimerType.PreciseTimer)
            self._timer.timeout.connect(self._on_feed)
        # Unrecorded: a sink that has never been written to is legitimately
        # empty, and calling that an underrun would put a false dropout in the
        # log at every start — the one thing that teaches an operator to stop
        # trusting these warnings.
        self._on_feed(record=False)
        self._timer.start()

    @pyqtSlot()
    def close_sink(self) -> None:
        if self._timer is not None:
            self._timer.stop()
        self._sink_device = None
        self.opened = False
        if self._sink is not None:
            try:
                self._sink.stop()
            except Exception as e:
                self.logger.warning(f"Error stopping audio sink: {e}")
            self._sink = None

    # -- internals, all on the audio thread ------------------------------
    def _resolve_device(self) -> QAudioDevice:
        if self.device_id:
            for device in QMediaDevices.audioOutputs():
                if device_id_str(device) == self.device_id:
                    return device
            self.logger.warning(
                f"Audio output '{self.device_id}' not found, using default")
        return QMediaDevices.defaultAudioOutput()

    def _try_format(self, device: QAudioDevice, fmt: QAudioFormat):
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

    def _on_feed(self, record: bool = True) -> None:
        """Top up the sink buffer with freshly mixed audio (AUDIO thread)."""
        if self._sink is None or self._sink_device is None:
            return
        free = self._sink.bytesFree()
        # The sink's own verdict on whether it ran dry. Idle in push mode means
        # the buffer emptied — Qt reports it that way rather than as an error
        # on some backends, so both are asked. This is the ONE place a dropout
        # is observable at all; nothing else in the app ever looked.
        if record:
            self.monitor.record_feed(
                underrun=(self._sink.error() == QAudio.Error.UnderrunError
                          or self._sink.state() == QAudio.State.IdleState),
                fill_sec=(max(0, self.buffer_bytes - free)
                          / self.bytes_per_frame / max(1, self.rate)))
        frames = free // self.bytes_per_frame
        if frames <= 0:
            return
        block = self.mixer.render(frames)
        if self.use_float:
            payload = block.astype(np.float32).tobytes()
        else:
            payload = (block * 32767.0).astype(np.int16).tobytes()
        self._sink_device.write(payload)


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
        # Timing flight recorder: the feed reports here every top-up, the
        # metronome every beat. Reading the sink is this class's job, so the
        # monitor is handed a verdict rather than the device.
        self.monitor = TimingMonitor()
        # Opt-in (TUPPERDMX_STALL_TRACE=1): names whatever blocks the Qt thread
        # by watching the feed's heartbeat from a thread of its own.
        self._attributor = maybe_start_attributor(self.monitor.last_feed)

        self._decoder = ThreadedAudioDecoder(self)
        self._decoder.decoded.connect(self._on_decoded)
        self._decoder.failed.connect(self._on_decode_failed)

        self._rate = SAMPLE_RATE   # the whole pipeline's rate; the sink picks it
        self._device_id = ""
        self._loaded_path: Optional[str] = None
        self._loading_path: Optional[str] = None

        # The sink and its top-up loop live on their own thread, above every
        # other priority in the app: the click is the one thing a show cannot
        # do without, and the stage must never be able to starve it.
        self._thread = QThread()
        self._thread.setObjectName("audio-feed")
        self._worker = _FeedWorker(self.mixer, self.monitor, self.logger)
        self._worker.moveToThread(self._thread)
        # Destroy the worker ON its own thread: its QTimer belongs there, and
        # a QObject torn down from elsewhere gets "Timers cannot be stopped
        # from another thread" and leaks the timer.
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start(QThread.Priority.TimeCriticalPriority)

        self._clicks = ClickSounds()

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
        if device_id == self._device_id and self._worker.opened:
            return
        self._device_id = device_id or ""
        self._start_sink()

    def set_routing(self, routing: str) -> None:
        self.mixer.set_routing(routing)

    def _call_worker(self, slot: str) -> None:
        """Run one of the worker's slots ON the audio thread and wait for it.

        Blocking on purpose: opening, retuning and feeding have to stay in that
        order, and a queued-but-not-waited call would let the pipeline retune
        underneath a sink that is already running."""
        if not self._thread.isRunning():
            return      # after cleanup: blocking on a dead loop would hang
        QMetaObject.invokeMethod(
            self._worker, slot, Qt.ConnectionType.BlockingQueuedConnection)

    def _start_sink(self) -> None:
        """Open the sink (on the audio thread), retune the pipeline to the rate
        it negotiated, then let it start feeding."""
        self._worker.device_id = self._device_id
        self._call_worker("open_sink")
        if not self._worker.opened:
            return
        # Safe to read the worker's fields here: it is idle between the two
        # blocking calls, and each one is a full memory barrier.
        self._apply_rate(self._worker.rate)
        self._call_worker("begin_feeding")

    def _stop_sink(self) -> None:
        self._call_worker("close_sink")

    def _apply_rate(self, rate: int) -> None:
        """Retune the whole mixing chain to the sink's rate.

        Everything loaded is at the old rate, so the clicks are regenerated,
        the decoder is re-targeted and a loaded song is resampled where it
        stands, keeping its position — the transport asks the mixer for
        seconds, so a rate switch mid-song stays inaudible beyond the sink
        restart. Called with the sink open but NOT yet feeding."""
        if rate == self._rate or rate <= 0:
            return
        previous, self._rate = self._rate, rate
        self._clicks.set_rate(rate)
        self._decoder.set_target_rate(rate)

        pcm = self.mixer.backing_samples()
        position = self.mixer.position_seconds()
        self.mixer.clear_oneshots()   # queued at the old rate
        self.mixer.set_sample_rate(rate)
        if pcm is not None:
            self.mixer.set_backing(resample(pcm, previous, rate))
            self.mixer.set_position_seconds(position)
        self.logger.info(f"Audio pipeline retuned {previous} Hz -> {rate} Hz")

    def rate(self) -> int:
        """The pipeline's sample rate — the sink's, once it has negotiated."""
        return self._rate

    def is_running(self) -> bool:
        """Whether an output sink is open and being fed."""
        return self._worker.opened

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
    def frames_rendered(self) -> int:
        """The output's own clock — what a caller schedules against."""
        return self.mixer.frames_rendered()

    def trigger_click(self, kind: str) -> None:
        """Sound a click kind as soon as possible (previews, tests)."""
        self.mixer.trigger(BUS_CLICK, self._clicks.sample(kind))

    def schedule_click(self, kind: str, at_frame: int) -> int:
        """Place a beep at an exact output frame; returns frames LATE (0 = on
        time). This is what makes the click deterministic: the beep's position
        stops depending on when the Qt thread got round to asking for it."""
        return self.mixer.schedule(
            BUS_CLICK, self._clicks.sample(kind), at_frame)

    def clear_clicks(self) -> None:
        """Drop the clicks queued ahead of the playhead — a stop or a seek
        invalidates that grid. A beep already sounding finishes, so stopping
        does not chop one in half."""
        self.mixer.clear_scheduled(BUS_CLICK)

    def schedule_voice(self, samples: np.ndarray, at_frame: int) -> int:
        """Place a voice clip at an exact output frame (the spoken count-in,
        which has to land ON its beat rather than near it)."""
        return self.mixer.schedule(
            BUS_VOICE, resample(samples, SAMPLE_RATE, self._rate), at_frame)

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
        """Idempotent shutdown. The sink is closed ON the audio thread and
        waited for before that thread is asked to quit: a queued close would
        race the quit, and the sink would be torn down by whichever got there
        first."""
        self.logger.info("Cleaning up audio engine")
        if self._attributor is not None:
            self._attributor.stop()
            self._attributor = None
        self._watchdog.stop()
        self._decoder.shutdown()
        if self._thread.isRunning():
            self._stop_sink()
            self._thread.quit()
            if not self._thread.wait(2000):
                self.logger.warning("Audio thread did not stop in 2 s")
