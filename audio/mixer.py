# ABOUTME: Qt-free stereo mixing core for the audio engine: backing/click/voice
# ABOUTME: buses, L/R routing modes, one-shot clips and per-bus RMS levels.

import threading
from typing import Dict, List, Optional, Tuple

import numpy as np

# Reference rate: what the mixer runs at unless the output device negotiates
# another one (AudioEngine adopts the device's rate when 44.1 kHz is refused —
# Windows shared-mode WASAPI is normally locked to its 48 kHz mix format), and
# the rate the TTS cache is written at.
SAMPLE_RATE = 44100

# Routing modes: how the three buses land on the stereo output.
# stereo              — everything on both channels (classic behaviour).
# split_backing_left  — backing downmixed mono on LEFT, click+voice on RIGHT.
# split_backing_right — mirrored.
ROUTING_STEREO = "stereo"
ROUTING_SPLIT_BACKING_LEFT = "split_backing_left"
ROUTING_SPLIT_BACKING_RIGHT = "split_backing_right"
ROUTING_MODES = (
    ROUTING_STEREO,
    ROUTING_SPLIT_BACKING_LEFT,
    ROUTING_SPLIT_BACKING_RIGHT,
)

BUS_BACKING = "backing"
BUS_CLICK = "click"
BUS_VOICE = "voice"
BUSES = (BUS_BACKING, BUS_CLICK, BUS_VOICE)

# Metronome beep frequencies in Hz. Hardcoded by design — tweak here if needed;
# intentionally not exposed via config or the Settings UI. High = downbeat
# (beat 1), low = the other beats.
METRONOME_HIGH_PITCH_HZ = 1200
METRONOME_LOW_PITCH_HZ = 800


def bus_channel_gains(routing: str) -> Dict[str, Tuple[float, float]]:
    """Per-bus (left, right) channel gains for a routing mode.

    In the split modes the backing is downmixed to mono before these gains
    apply (a stereo track collapsed onto one channel must not lose a side)."""
    if routing == ROUTING_SPLIT_BACKING_LEFT:
        return {BUS_BACKING: (1.0, 0.0), BUS_CLICK: (0.0, 1.0), BUS_VOICE: (0.0, 1.0)}
    if routing == ROUTING_SPLIT_BACKING_RIGHT:
        return {BUS_BACKING: (0.0, 1.0), BUS_CLICK: (1.0, 0.0), BUS_VOICE: (1.0, 0.0)}
    return {BUS_BACKING: (1.0, 1.0), BUS_CLICK: (1.0, 1.0), BUS_VOICE: (1.0, 1.0)}


def make_beep(freq_hz: float, duration_sec: float = 0.1,
              sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Mono float32 sine beep with a short fade to avoid edge clicks."""
    n_samples = int(duration_sec * sample_rate)
    t = np.arange(n_samples, dtype=np.float32) / sample_rate
    wave = np.sin(2 * np.pi * freq_hz * t).astype(np.float32)
    fade = min(n_samples // 4, int(0.005 * sample_rate))
    if fade > 0:
        wave[:fade] *= np.linspace(0.0, 1.0, fade, dtype=np.float32)
        wave[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)
    return wave


def resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linear-resample mono (n,) or interleaved (n, channels) audio, keeping
    the input dtype. Returns the input untouched when the rates match.

    Linear is good enough here and stays cheap: every caller converts a whole
    buffer (a decoded song, a TTS clip) off the audio thread, never per block."""
    if (src_rate == dst_rate or src_rate <= 0 or dst_rate <= 0
            or samples is None or len(samples) < 2):
        return samples
    n_out = max(1, int(round(len(samples) * dst_rate / src_rate)))
    src_t = np.arange(len(samples))
    dst_t = np.linspace(0, len(samples) - 1, n_out)
    if samples.ndim == 1:
        out = np.interp(dst_t, src_t, samples)
    else:
        out = np.stack([np.interp(dst_t, src_t, samples[:, ch])
                        for ch in range(samples.shape[1])], axis=1)
    return out.astype(samples.dtype)


class _OneShot:
    """A mono clip being played once on a bus, starting at an absolute OUTPUT
    FRAME. That frame is what makes a click exact: the beep is placed inside
    the block rather than at whatever moment the Qt thread got round to asking
    for it. A clip spanning two blocks re-points `at_frame` at the next block's
    start, so the same field carries 'begins here' and 'continues here'."""
    __slots__ = ("samples", "pos", "at_frame")

    def __init__(self, samples: np.ndarray, at_frame: int):
        self.samples = samples
        self.pos = 0
        self.at_frame = at_frame


class Mixer:
    """Three-bus stereo mixer. `render()` is called from the audio feed
    thread; every mutator takes the same lock, so state changes are safe from
    the Qt thread. Backing is int16 (n, 2) at `sample_rate`; one-shots are mono
    float32 at the same rate."""

    def __init__(self, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self._lock = threading.Lock()
        self._routing = ROUTING_STEREO
        self._gains = bus_channel_gains(self._routing)
        self._volume = {bus: 1.0 for bus in BUSES}
        self._muted = {bus: False for bus in BUSES}
        self._backing: Optional[np.ndarray] = None
        self._cursor = 0                 # backing position in frames
        self._backing_active = False     # advances/plays only while True
        self._backing_ended = False      # latched when the cursor crosses the end
        self._oneshots: Dict[str, List[_OneShot]] = {BUS_CLICK: [], BUS_VOICE: []}
        self._levels = {bus: 0.0 for bus in BUSES}
        # Monotonic count of frames this mixer has rendered: the output's own
        # clock, and the only timebase a one-shot is scheduled against. It
        # never resets — a seek moves the backing cursor, not the output.
        self._frames_rendered = 0

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    def set_routing(self, routing: str) -> None:
        if routing not in ROUTING_MODES:
            routing = ROUTING_STEREO
        with self._lock:
            self._routing = routing
            self._gains = bus_channel_gains(routing)

    def set_sample_rate(self, sample_rate: int) -> None:
        """Retune to the rate the output device negotiated. Everything already
        loaded is at the OLD rate, so the caller re-supplies the backing and
        drops queued one-shots — see AudioEngine._apply_rate()."""
        with self._lock:
            self.sample_rate = max(1, int(sample_rate))

    def routing(self) -> str:
        with self._lock:
            return self._routing

    def set_volume(self, bus: str, volume: float) -> None:
        with self._lock:
            self._volume[bus] = max(0.0, min(1.0, volume))

    def set_muted(self, bus: str, muted: bool) -> None:
        with self._lock:
            self._muted[bus] = bool(muted)

    # ------------------------------------------------------------------
    # Backing track
    # ------------------------------------------------------------------
    def set_backing(self, samples: Optional[np.ndarray]) -> None:
        """Install the decoded song (int16 (n, 2)) or None to unload."""
        with self._lock:
            self._backing = samples
            self._cursor = 0
            self._backing_ended = False

    def has_backing(self) -> bool:
        with self._lock:
            return self._backing is not None

    def backing_samples(self) -> Optional[np.ndarray]:
        """The installed song, or None. set_backing() swaps the array whole
        and nothing mutates it in place, so a reader on another thread may
        keep using the returned reference after the lock is dropped."""
        with self._lock:
            return self._backing

    def duration_seconds(self) -> float:
        with self._lock:
            if self._backing is None:
                return 0.0
            return len(self._backing) / self.sample_rate

    def set_backing_active(self, active: bool) -> None:
        with self._lock:
            self._backing_active = bool(active)

    def backing_active(self) -> bool:
        with self._lock:
            return self._backing_active

    def set_position_seconds(self, seconds: float) -> None:
        with self._lock:
            self._cursor = max(0, int(seconds * self.sample_rate))
            self._backing_ended = False

    def position_seconds(self) -> float:
        with self._lock:
            return self._cursor / self.sample_rate

    def consume_backing_ended(self) -> bool:
        """Return (and clear) the end-of-backing latch."""
        with self._lock:
            ended = self._backing_ended
            self._backing_ended = False
            return ended

    # ------------------------------------------------------------------
    # One-shots (click beeps, voice clips)
    # ------------------------------------------------------------------
    def frames_rendered(self) -> int:
        """Frames of output produced so far — the anchor a caller schedules
        against. Read from the Qt thread while the audio thread advances it,
        hence the lock."""
        with self._lock:
            return self._frames_rendered

    def trigger(self, bus: str, samples: np.ndarray) -> None:
        """Queue a mono float32 clip to start in the NEXT rendered block."""
        with self._lock:
            self._oneshots[bus].append(
                _OneShot(samples, self._frames_rendered))

    def schedule(self, bus: str, samples: np.ndarray, at_frame: int) -> int:
        """Queue a clip to start at absolute output frame `at_frame`.

        Returns how many frames LATE the request was: 0 when the frame is
        still ahead of the output (the normal case, and the clip then lands
        exactly there), positive when the playhead has already passed it, in
        which case the clip starts as soon as possible rather than being
        dropped — a late click is still better than a missing one, and the
        number is what tells the caller its lookahead was not enough."""
        with self._lock:
            late = max(0, self._frames_rendered - int(at_frame))
            self._oneshots[bus].append(
                _OneShot(samples, max(int(at_frame), self._frames_rendered)))
            return late

    def clear_oneshots(self, bus: Optional[str] = None) -> None:
        with self._lock:
            for name in self._oneshots:
                if bus is None or name == bus:
                    self._oneshots[name] = []

    def clear_scheduled(self, bus: str) -> None:
        """Drop the clips queued for the FUTURE, keeping any already sounding.

        A stop invalidates the beats placed ahead of the playhead, but cutting
        the beep that is half-way out of the speaker turns a clean stop into a
        pop — so a clip that has started is allowed to finish."""
        with self._lock:
            self._oneshots[bus] = [shot for shot in self._oneshots[bus]
                                   if shot.pos > 0]

    # ------------------------------------------------------------------
    # Levels
    # ------------------------------------------------------------------
    def levels(self) -> Dict[str, float]:
        """Post-volume RMS of each bus in the last rendered block (0..1)."""
        with self._lock:
            return dict(self._levels)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def render(self, frames: int) -> np.ndarray:
        """Mix `frames` frames of stereo float32 output."""
        with self._lock:
            out = np.zeros((frames, 2), dtype=np.float32)
            block_start = self._frames_rendered

            # Backing bus
            backing_level = 0.0
            volume = 0.0 if self._muted[BUS_BACKING] else self._volume[BUS_BACKING]
            if self._backing is not None and self._backing_active:
                total = len(self._backing)
                start = self._cursor
                end = min(start + frames, total)
                if start < end:
                    block = np.zeros((frames, 2), dtype=np.float32)
                    block[: end - start] = (
                        self._backing[start:end].astype(np.float32) / 32768.0
                    )
                    gain_l, gain_r = self._gains[BUS_BACKING]
                    if self._routing == ROUTING_STEREO:
                        out += block * volume
                        backing_level = _rms(block.mean(axis=1) * volume)
                    else:
                        mono = block.mean(axis=1) * volume
                        out[:, 0] += mono * gain_l
                        out[:, 1] += mono * gain_r
                        backing_level = _rms(mono)
                if start < total <= start + frames:
                    self._backing_ended = True
                self._cursor = start + frames
            self._levels[BUS_BACKING] = backing_level

            # One-shot buses, each clip placed at its own frame within the
            # block — that sub-block offset IS the click's accuracy.
            for bus in (BUS_CLICK, BUS_VOICE):
                volume = 0.0 if self._muted[bus] else self._volume[bus]
                buf = np.zeros(frames, dtype=np.float32)
                alive: List[_OneShot] = []
                for shot in self._oneshots[bus]:
                    offset = shot.at_frame - block_start
                    if offset >= frames:
                        alive.append(shot)     # still in the future
                        continue
                    start = max(0, offset)
                    take = min(frames - start, len(shot.samples) - shot.pos)
                    if take > 0:
                        buf[start:start + take] += (
                            shot.samples[shot.pos:shot.pos + take])
                        shot.pos += take
                    if shot.pos < len(shot.samples):
                        shot.at_frame = block_start + frames   # resumes there
                        alive.append(shot)
                self._oneshots[bus] = alive
                buf *= volume
                gain_l, gain_r = self._gains[bus]
                out[:, 0] += buf * gain_l
                out[:, 1] += buf * gain_r
                self._levels[bus] = _rms(buf)

            self._frames_rendered = block_start + frames
            np.clip(out, -1.0, 1.0, out)
            return out


def _rms(signal: np.ndarray) -> float:
    if signal.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(signal))))
