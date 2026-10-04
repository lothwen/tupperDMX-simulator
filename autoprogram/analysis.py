# ABOUTME: Whole-song audio analysis — the beat grid, bars, sections and their
# ABOUTME: materials, climax, ending, builds, impacts, stops, accents, key —
# ABOUTME: mapped from a decoded recording into a SongMap. Qt-free.
"""Map a song's structure from its audio.

Runs streaming.py's live analysers over the song (in streaming.bench's
order), then reads the whole recording at once, which a live analyser cannot:

  * GRID      one tempo and phase fitted to the onset envelope of the entire
              song, searched around the song's own BPM (or the live tracker's
              estimate when the song has none). The live tracker wanders by an
              octave-ish on syncopated passages; the whole-song fit does not.
  * DOWNBEAT  the beat of the bar carrying the strongest kick and onsets, and
              where the chords change.
  * BARS      loudness, the per-beat envelope, the downbeat kick and an
              ENERGY (loudness, onset density, brightness and low end, ranked
              against the rest of the song) per bar.
  * VOICE     how surely a voice sings in each bar: partials whose pitch
              glides and wavers the way a voice's does and a riff's does not,
              and the low mids a voice fills.
  * SECTIONS  where the sound (timbre) or the chords change and where the
              singing starts or stops, cut into phrases that run 4 or 8 bars
              on the song's 8-bar grid where the music allows. Phrases that
              play the same music share a MATERIAL letter — every chorus is
              the same letter — and a run of one material at one loudness is
              one section. What is sung is a VERSE, a CHORUS or a BRIDGE;
              what is not an INTRO, an INST, a BREAK or an OUTRO (a song with
              no voice goes by its loudness instead).
  * CLIMAX    the section the song builds to: its last loud one, unless that
              one is clearly weaker than the song's strongest. A chorus climax
              the song ends on — a few quiet bars at most after it, its last
              hit or the ring of it — is the OUTRO the arrangement band marks,
              and END starts where it ends (SongMap.parts).
  * ENDING    how the sound stops — dead on a last hit (cut), a last hit
              left ringing (ring), or dying away (fade).
  * BUILDS    bars that crescendo into a louder downbeat.
  * IMPACTS   the hit each build lands on, and any sudden jump in loudness.
  * STOPS     the band dropping out for a moment inside a loud passage.
  * ACCENTS   the strongest onsets inside quiet passages.
  * HITS      the beats of a section's bar its snare / cymbals lean on.
  * KEY       the key and mode the chroma of the whole song correlates best with.

Every time is AUDIO seconds; a caller anchoring clips adds the track's offset.
The input is a mono signal normalised to a peak of 1 — decode.py makes one
from a file with the app's own decoder.
"""
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from autoprogram import streaming

# Loudness levels a bar can sit at, relative to the song's loud reference.
SILENT, QUIET, MID, HIGH = 0, 1, 2, 3
LEVEL_NAMES = ("silent", "quiet", "mid", "high")
# Below SILENT_MAX a bar is silent, below QUIET_MAX quiet, from HIGH_MIN up
# high, mid between — fractions of the loud reference (the 90th percentile of
# bar loudness). A build must be at least BUILD_MIN loud.
SILENT_MAX, QUIET_MAX, HIGH_MIN, BUILD_MIN = 0.12, 0.5, 0.68, 0.2
MIN_SECTION_BARS = 2

# Structure. A phrase boundary scores the change in timbre and chords across
# it (NOVELTY_SPAN bars either side), plus a bonus where a build lands, the
# loudness turns or a voice starts or stops singing. Cutting the song costs
# SEGMENT_COST a phrase, and a phrase of 8 bars (or 16, 24...) earns PHRASE8,
# one of 4 PHRASE4, an odd one loses ODD_PHRASE. A song keeps one grid of
# 8-bar lines through all of it, so a cut on one earns GRID8, on the 4-bar
# line between GRID4, off the 2-bar lines OFF_GRID — the lengths and the
# grid that hand-placed section markers follow, on the songs this was tuned
# on (Directo-2027.show). Two phrases are one MATERIAL when they are
# SAME_MATERIAL standard deviations more alike than the song's phrases are
# on average, and a voice sings both or neither. Where a voice sings, that
# already keeps the intro off the verse, and a chorus sung again with other
# words is still the chorus: there SAME_MATERIAL_SUNG.
NOVELTY_SPAN = 8
SEGMENT_COST, PHRASE8, PHRASE4, ODD_PHRASE = 2.5, 2.0, 0.6, -1.5
GRID8, GRID4, OFF_GRID = 3.0, 1.0, -1.5
TURN_BONUS = 0.5
SAME_MATERIAL, SAME_MATERIAL_SUNG = 0.8, 0.0
# Singing. A voice never holds a pitch the way a fret or a key does: it
# glides into notes and wavers on them (vibrato). voice() measures that, the
# low mids a voice fills and the level; VOICE_WEIGHTS (glide, tilt, level,
# bias) turn them, each against the song's other bars, into how surely a bar
# is sung — fitted to vocal stems a source-separation model made of the
# tuning songs, which they agree with on 85 % of the bars. A song whose
# partials glide under GLIDE_FLOOR is instrumental. A change in singing
# across a boundary adds VOICE_TURN to its score.
VOICE_BAND = (150.0, 5000.0)
GLIDE_MIN, GLIDE_MAX = 3.0, 40.0
REPEAT_BARS = 4
VOICE_WEIGHTS = (1.97, -0.58, 0.37, 1.64)
GLIDE_FLOOR = 0.12
VOICE_TURN = 2.0
# A climax at least this much of the strongest loud section's energy. A chorus
# climax with no more than TAIL_BARS quiet bars after it is the song's OUTRO.
CLIMAX_SHARE = 0.8
TAIL_BARS = 4
# A stop: the sound under STOP_DEPTH of the song's loud level for at least
# STOP_BEATS beats (and STOP_MIN seconds), with loud music either side.
STOP_DEPTH, STOP_BEATS, STOP_MIN = 0.15, 0.5, 0.25
# An ending is a cut when the sound is gone within CUT_TAIL seconds of the
# last hit.
CUT_TAIL = 1.0
# The grid's tempo is searched within GRID_SEARCH of each candidate BPM (the
# song's own, the live tracker's). A grid fitting under STEADY_FIT is no
# steady beat. A bar is a BUILD when its last beat is BUILD_RISE times as
# loud as its first and it lands on a louder downbeat.
GRID_SEARCH = 0.03
STEADY_FIT = 2.0
BUILD_RISE = 1.4


@dataclass(frozen=True)
class Tuning:
    """The thresholds a user may move (the app's Advanced panel). The
    defaults are the values above, the ones the analysis was tuned with."""
    grid_search: float = GRID_SEARCH
    quiet_max: float = QUIET_MAX
    high_min: float = HIGH_MIN
    build_rise: float = BUILD_RISE
    segment_cost: float = SEGMENT_COST
    climax_share: float = CLIMAX_SHARE


# Krumhansl-Kessler key profiles, from the tonic up.
_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
_NOTES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

@dataclass
class Bar:
    number: int                   # 1-based, bar 1 holds the first downbeat
    start: float                  # audio seconds (may be < 0 for a pickup bar)
    loud: float                   # mean RMS / the song's loud reference
    beats: List[float]            # mean RMS of each beat, same scale
    kick: float                   # bass-onset strength at the downbeat
    level: int = QUIET            # SILENT / QUIET / MID / HIGH
    build: bool = False           # crescendos into the next downbeat
    energy: float = 0.0           # 0-1, against the song's other bars
    voice: float = 0.0            # how surely a voice sings in it, 0-1


@dataclass
class Section:
    part: str                     # what it plays: INTRO / VERSE / CHORUS / BRIDGE / INST /
                                  # BREAK / OUTRO (the band may mark it otherwise: SongMap.parts)
    level: int
    first: int                    # first bar number
    last: int                     # last bar number (inclusive)
    start: float
    end: float
    material: str = "A"           # one letter per distinct music: repeats share it
    energy: float = 0.0           # mean of its bars'
    sung: bool = False            # a voice sings through most of it
    phrases: List[int] = field(default_factory=list)  # bars its phrases start on
    hits: List[float] = field(default_factory=list)   # snare beats of its bar (1-based)


@dataclass
class SongMap:
    duration: float
    bpm: float                    # quarter-note BPM of the fitted grid
    beat: float                   # seconds per beat of the meter's own note
    beats_per_bar: int
    first_downbeat: float
    fit: float                    # grid alignment: mean onset strength on it
                                  # over the mean off it (1 = no better than chance)
    tracker_bpm: Optional[float]  # what the live tracker settled on
    bars: List[Bar]
    sections: List[Section]
    impacts: List[Tuple[float, float]] = field(default_factory=list)  # (time, strength)
    accents: List[Tuple[float, float]] = field(default_factory=list)  # (time, strength)
    end_of_sound: float = 0.0
    warnings: List[str] = field(default_factory=list)
    climax: Optional[int] = None   # index into sections
    outro: Optional[int] = None    # the climax, when it is a chorus the song ends on
    ending: str = "fade"           # cut / ring / fade
    final_hit: Optional[float] = None  # the last hit of a cut or ring ending
    cut: Optional[float] = None    # when the sound actually stops
    stops: List[Tuple[float, float]] = field(default_factory=list)  # (from, to)
    key: str = ""                  # e.g. "A minor"
    # bar ranges (first, last) the grid does not fit, in a song it otherwise does
    weak_grid: List[Tuple[int, int]] = field(default_factory=list)

    @property
    def bar_length(self) -> float:
        return self.beat * self.beats_per_bar

    def bar_at(self, t: float) -> Bar:
        i = int(math.floor((t - self.bars[0].start) / self.bar_length))
        return self.bars[max(0, min(len(self.bars) - 1, i))]

    def time(self, bar: int, beat: float = 1, sixteenth: int = 1) -> float:
        """Audio time of bar.beat.16th (all 1-based) on the grid."""
        return (self.bars[0].start + (bar - 1) * self.bar_length
                + (beat - 1) * self.beat + (sixteenth - 1) * self.beat / 4)

    def section_of(self, bar: int) -> Optional[Section]:
        return next((s for s in self.sections if s.first <= bar <= s.last), None)

    def parts(self) -> List[Tuple[str, Section]]:
        """Each section with the song part the arrangement band marks it:
        the part it plays, but OUTRO for the chorus climax the song ends on
        and END for the quiet tail after it, which the END from end_start
        covers."""
        return [(s.part if self.outro is None or k < self.outro else
                 "OUTRO" if k == self.outro else "END", s)
                for k, s in enumerate(self.sections)]

    @property
    def end_start(self) -> float:
        """Where the END song part starts: where the OUTRO the song ends on
        ends, else where the sound does."""
        return self.sections[self.outro].end if self.outro is not None else self.end_of_sound

    @property
    def minor(self) -> bool:
        return self.key.endswith("minor")


# ---------------------------------------------------------------------------
# The live analysers, over the whole song
# ---------------------------------------------------------------------------
def run_analysers(sr: int, mono: np.ndarray) -> dict:
    """The live analysers over the whole song, in the order
    streaming.bench runs them, recording every frame instead of
    printing a status line once a second."""
    fps = sr / streaming.HOP
    bands = streaming.filterbank(sr, mono)

    onset_det = streaming.OnsetDetector(fps)
    tempo_est = streaming.TempoEstimator(fps)
    env_total = streaming.EnvFollower(fps)
    env_bands = [streaming.EnvFollower(fps) for _ in bands]

    frames, onsets, tempos = [], [], []
    last_tempo = 0.0
    for i in range((len(mono) - streaming.FRAME) // streaming.HOP + 1):
        pos = i * streaming.HOP
        t = (pos + streaming.FRAME / 2) / sr
        frame = mono[pos:pos + streaming.FRAME]
        flux, _mag, onset = onset_det.process(frame, t)
        tempo_est.add(flux)
        rms = float(np.sqrt(np.mean(frame[:streaming.HOP] ** 2)))
        total = env_total.process(rms)
        levels = [f.process(float(np.sqrt(np.mean(b[pos:pos + streaming.HOP] ** 2))))
                  for f, b in zip(env_bands, bands)]
        if onset:
            onsets.append(onset)
        if t - last_tempo >= 0.5:
            last_tempo = t
            est = tempo_est.estimate()
            if est:
                tempos.append((t, *est))
        frames.append((t, rms, flux, total, *levels))

    f = np.array(frames, dtype=np.float64)
    return {"fps": fps, "t": f[:, 0], "rms": f[:, 1], "flux": f[:, 2],
            "env": f[:, 3], "bass": f[:, 4], "high": f[:, 6],
            "onsets": onsets, "tempos": tempos}


def spectral(sr: int, mono: np.ndarray) -> dict:
    """Chroma, timbre and brightness every ~46 ms, from a 4096-point STFT of
    the signal at a quarter of its rate — fine enough to tell semitones apart
    down to the bass. Timbre is the cepstrum of 24 log-spaced bands, without
    its level (the first coefficient)."""
    fs = sr / 4
    x = _decimate(mono, 4).astype(np.float32)
    f, t, z = _stft(x, fs, 4096, 512)
    power = np.abs(z) ** 2
    keep = (f >= 60) & (f <= 4000)
    pitch_class = np.round(69 + 12 * np.log2(f[keep] / 440.0)).astype(int) % 12
    tone = np.log1p(100 * np.sqrt(power[keep]))
    chroma = np.stack([tone[pitch_class == k].sum(axis=0) for k in range(12)])
    edges = np.geomspace(40, fs / 2 - 1, 25)
    bands = np.stack([power[(f >= lo) & (f < hi)].sum(axis=0)
                      for lo, hi in zip(edges, edges[1:])])
    timbre = _dct(np.log(bands + 1e-9), 14)[1:]
    centroid = (f[:, None] * power).sum(axis=0) / (power.sum(axis=0) + 1e-12)
    return {"t": t, "chroma": chroma, "timbre": timbre, "centroid": centroid}


def voice(sr: int, mono: np.ndarray, bar_len: float) -> dict:
    """What tells a voice apart, every ~12 ms (``t``), from a 1024-point
    STFT of the signal at half its rate:

      * ``glide``  the share of the harmonic partials' energy (VOICE_BAND)
                   whose pitch moves like a voice's and that the bars around
                   do not play. A partial is a spectral peak in a bin the
                   harmonic half of the spectrum (a median across time)
                   owns, for three frames running, its frequency reassigned
                   from the window's derivative; it glides when it moves
                   between GLIDE_MIN and GLIDE_MAX cents a frame, both
                   frames the same way — vibrato and portamento do, a
                   fretted or keyed note holds still, noise jumps. What the
                   bars around play at the same place in theirs (their
                   median, REPEAT_BARS either side) is the band's riff,
                   however much a chorus pedal makes it waver.
      * ``tilt``   the bass's share of the energy over that of 250-1000 Hz,
                   in dB: a voice fills the low mids.
      * ``level``  the energy, in dB."""
    size, hop = 1024, 256
    fs = sr / 2
    x = _decimate(mono, 2).astype(np.float32)
    freqs = np.fft.rfftfreq(size, 1 / fs)
    band = np.nonzero((freqs >= VOICE_BAND[0]) & (freqs <= VOICE_BAND[1]))[0]
    lo, hi = band[0] - 4, band[-1] + 5            # room for the median across frequency
    low, mids = (freqs >= 60) & (freqs < 250), (freqs >= 250) & (freqs < 1000)
    n = np.arange(size)
    window = (0.5 - 0.5 * np.cos(2 * np.pi * n / size)).astype(np.float32)
    slope = (np.pi / size * np.sin(2 * np.pi * n / size)).astype(np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(x, size)[::hop]
    mag, inst, energy = [], [], []
    for k in range(0, len(frames), 4096):
        block = frames[k:k + 4096]
        z = np.fft.rfft(block * window, axis=-1)
        power = np.abs(z) ** 2
        energy.append(np.stack([power.sum(axis=1), power[:, low].sum(axis=1),
                                power[:, mids].sum(axis=1)]))
        z, power = z[:, lo:hi], power[:, lo:hi]
        dz = np.fft.rfft(block * slope, axis=-1)[:, lo:hi]
        # reassigned frequency, in bins
        inst.append((np.arange(lo, hi) - np.imag(dz * np.conj(z)) / (power + 1e-20)
                     * size / (2 * np.pi)).astype(np.float32))
        mag.append(np.sqrt(power).astype(np.float32))
    mag, inst, energy = np.concatenate(mag), np.concatenate(inst), np.concatenate(energy, axis=1)
    across, along = _median(mag, 9, 0), _median(mag, 9, 1)
    harmonic = (across > along)[:, 4:-4]
    tonal = (mag * across ** 2 / (across ** 2 + along ** 2 + 1e-20))[:, 4:-4]
    mag, inst = mag[:, 4:-4], inst[:, 4:-4]
    count = len(mag)
    shifts = [int(round(k * bar_len * fs / hop)) for k in range(-REPEAT_BARS, REPEAT_BARS + 1) if k]
    riff = np.empty_like(tonal)
    for k in range(0, count, 512):
        rows = np.arange(k, min(count, k + 512))
        riff[rows] = np.median([tonal[np.clip(rows + s, 0, count - 1)] for s in shifts], axis=0)
    bins = np.arange(lo + 4, hi - 4)
    peak = np.zeros(mag.shape, dtype=bool)
    peak[:, 1:-1] = (mag[:, 1:-1] > mag[:, :-2]) & (mag[:, 1:-1] >= mag[:, 2:])
    floor = mag.max(axis=1, keepdims=True) * 0.01             # 40 dB under the frame's peak
    partial = peak & harmonic & (mag > floor) & (np.abs(inst - bins) < 0.6)
    cents = 1200 * np.log2(np.maximum(inst, 1.0))
    before, after = cents[1:-1] - cents[:-2], cents[2:] - cents[1:-1]
    held = mag[1:-1] ** 2 * (partial[:-2] & partial[1:-1] & partial[2:])
    glides = ((np.abs(before) > GLIDE_MIN) & (np.abs(before) < GLIDE_MAX)
              & (np.abs(after) > GLIDE_MIN) & (np.abs(after) < GLIDE_MAX)
              & (np.sign(before) == np.sign(after)) & (tonal > 1.5 * riff)[1:-1])
    glide = (held * glides).sum(axis=1) / (held.sum(axis=1) + 1e-12)
    total, bass, mid = (10 * np.log10(e[1:-1] + 1e-12) for e in energy)
    return {"t": (size / 2 + hop * np.arange(1, count - 1)) / fs, "glide": glide,
            "tilt": bass - mid, "level": total}


# ---------------------------------------------------------------------------
# Signal helpers (numpy only: the runtime carries no scipy)
# ---------------------------------------------------------------------------
def _decimate(x: np.ndarray, q: int) -> np.ndarray:
    """Every `q`-th sample of `x` low-passed below the new Nyquist without
    delay: the 20q + 1 tap Hamming-windowed sinc scipy.signal.decimate's
    "fir" filter uses, centred on each kept sample."""
    taps = np.sinc(np.arange(-10 * q, 10 * q + 1) / q) * np.hamming(20 * q + 1)
    return np.convolve(x, taps / taps.sum(), mode="same")[::q]


def _stft(x: np.ndarray, fs: float, size: int, hop: int):
    """``(freqs, times, spectra)`` of `x` cut into periodic-Hann windows of
    `size` every `hop` samples, each the window's centre and scaled to its
    spectrum (freqs x frames) — scipy.signal.stft with no boundary padding."""
    window = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(size) / size)).astype(x.dtype)
    frames = np.lib.stride_tricks.sliding_window_view(x, size)[::hop]
    spectra = np.fft.rfft(frames * window, axis=-1).T / window.sum()
    return (np.fft.rfftfreq(size, 1 / fs), (size / 2 + hop * np.arange(len(frames))) / fs,
            spectra)


def _dct(x: np.ndarray, count: int) -> np.ndarray:
    """The first `count` coefficients of the orthonormal DCT-II of `x` along
    its first axis."""
    n = x.shape[0]
    basis = np.cos(np.pi * np.arange(count)[:, None] * (2 * np.arange(n) + 1) / (2 * n))
    basis *= np.sqrt(2 / n)
    basis[0] /= np.sqrt(2)
    return basis @ x


def _median(x: np.ndarray, size: int, axis: int) -> np.ndarray:
    """The median of the `size` values around each one of `x` along `axis`
    (0 or 1 of a 2-D array), the edges repeated — scipy.ndimage's
    median_filter along one axis with mode="nearest". In blocks of rows, so
    a whole song never makes one `size`-times copy of itself."""
    half = size // 2
    pad = [(half, half), (0, 0)] if axis == 0 else [(0, 0), (half, half)]
    padded = np.pad(x, pad, mode="edge")
    out = np.empty_like(x)
    for k in range(0, len(x), 1024):
        rows = padded[k:k + 1024 + 2 * half] if axis == 0 else padded[k:k + 1024]
        out[k:k + 1024] = np.median(np.lib.stride_tricks.sliding_window_view(rows, size, axis=axis),
                                    axis=-1)
    return out


def _running_mean(x, size: int) -> np.ndarray:
    """The mean of the `size` values around each one of `x` — from size // 2
    before it — mirrored at the ends (edge sample repeated), as
    scipy.ndimage.uniform_filter1d gives it."""
    before = size // 2
    padded = np.pad(np.asarray(x, dtype=float), (before, size - 1 - before), mode="symmetric")
    sums = np.cumsum(np.concatenate(([0.0], padded)))
    return (sums[size:] - sums[:-size]) / size


# ---------------------------------------------------------------------------
# Whole-song reading
# ---------------------------------------------------------------------------
def _tracker_bpm(tempos) -> Optional[float]:
    """The live tracker's settled answer: the confidence-weighted median of
    its estimates once it has warmed up."""
    est = [(bpm, conf) for t, bpm, conf in tempos if t > 8.0 and conf >= 0.3]
    if not est:
        est = [(bpm, conf) for _t, bpm, conf in tempos]
    if not est:
        return None
    est.sort()
    weights = np.cumsum([c for _b, c in est])
    return float(est[int(np.searchsorted(weights, weights[-1] / 2))][0])


def _grid_score(t, strength, period, phase, lo, hi):
    grid = np.arange(phase, hi, period)
    grid = grid[grid >= lo]
    return float(np.interp(grid, t, strength).mean()) if len(grid) else 0.0


def fit_grid(a: dict, around: float, beat_scale: float, lo: float, hi: float,
             search: float = GRID_SEARCH):
    """``(bpm, phase, fit)``: the quarter-note BPM within `search` (a
    fraction) of `around` and the phase whose grid lands on the most onset
    strength between lo and hi."""
    t = a["t"]
    strength = np.maximum(a["flux"] - _running_mean(a["flux"], int(a["fps"] * 0.5)), 0)
    best = (-1.0, around, 0.0)
    for bpm in np.arange(around * (1 - search), around * (1 + search), 0.02):
        period = 60.0 / bpm * beat_scale
        for phase in np.linspace(0, period, 48, endpoint=False):
            s = _grid_score(t, strength, period, phase, lo, hi)
            if s > best[0]:
                best = (s, float(bpm), float(phase))
    _s, bpm, phase = best
    period = 60.0 / bpm * beat_scale
    phases = np.linspace(phase - period / 48, phase + period / 48, 41)
    scores = [_grid_score(t, strength, period, p, lo, hi) for p in phases]
    phase = float(phases[int(np.argmax(scores))]) % period
    on = max(scores)
    off = _grid_score(t, strength, period, phase + period / 2, lo, hi)
    return bpm, phase, on / off if off > 0 else float("inf")


def _window_max(x, t, centre, before, after):
    m = (t >= centre - before) & (t < centre + after)
    return float(x[m].max()) if m.any() else 0.0


def _window_mean(x, t, start, end):
    m = (t >= start) & (t < end)
    return float(x[m].mean()) if m.any() else 0.0


def analyse_samples(sr: int, mono: np.ndarray, bpm: Optional[float] = None,
                    meter: str = "4/4", tuning: Tuning = Tuning()) -> SongMap:
    """Map a mono signal normalised to a peak of 1."""
    a = run_analysers(sr, mono)
    t, rms, env, bass = a["t"], a["rms"], a["env"], a["bass"]
    duration = len(mono) / sr
    num, den = (int(x) for x in meter.split("/"))
    beat_scale = 4.0 / den                       # BPM counts quarters

    # where there is sound at all
    loud_frames = np.nonzero(env > 0.01)[0]
    sound_from = float(t[loud_frames[0]]) if len(loud_frames) else 0.0
    sound_to = float(t[loud_frames[-1]]) if len(loud_frames) else duration

    # The song's BPM and the live tracker's both get a fit; the better grid
    # wins. A BPM typed for some other take of the song loses to the audio.
    tracker = _tracker_bpm(a["tempos"])
    warnings: List[str] = []
    candidates = [c for c in (bpm, tracker) if c] or [120.0]
    if len(candidates) == 2 and abs(candidates[0] / candidates[1] - 1) < 0.03:
        candidates = candidates[:1]
    fits = [fit_grid(a, c, beat_scale, sound_from, sound_to, tuning.grid_search)
            for c in candidates]
    bpm_fit, phase, fit = max(fits, key=lambda x: x[2])
    if fit < STEADY_FIT:
        warnings.append(f"no steady beat found (grid fit {fit:.1f}x): cues follow the "
                        f"loudness, but may not land on beats")
    if bpm and len(fits) == 2 and fits[1][2] > fits[0][2]:
        warnings.append(f"the audio is not at the song's {bpm:g} BPM: the grid fits "
                        f"{fits[1][0]:.1f} ({fits[1][2]:.1f}x vs {fits[0][2]:.1f}x)")
    beat = 60.0 / bpm_fit * beat_scale

    # downbeat: the beat of the bar the kick and the onsets lean on, and the
    # chords change on — a kick on every beat leaves only the chords to tell
    spec = spectral(sr, mono)
    bass_onset = np.maximum(np.diff(bass, prepend=bass[0]), 0)
    strength = np.maximum(a["flux"] - _running_mean(a["flux"], int(a["fps"] * 0.5)), 0)
    beats = np.arange(phase, duration, beat)
    kick = np.array([_window_max(bass_onset, t, b, 0.03, 0.06) for b in beats])
    hit = np.array([_window_max(strength, t, b, 0.03, 0.06) for b in beats])
    change = _chord_change(_span_chroma(spec, beats, beat))
    # a lone change (a section turning) must not outweigh the kick: only
    # changes that keep coming back count, so the curve is clipped
    change = np.minimum(change, np.percentile(change, 90))
    weight = (kick / (kick.mean() + 1e-12) + hit / (hit.mean() + 1e-12)
              + 2 * change / (change.mean() + 1e-12))
    first = int(np.argmax([weight[k::num].mean() for k in range(num)]))
    downbeat = phase + first * beat
    bar_len = beat * num
    while downbeat - bar_len > sound_from - bar_len / 2:
        downbeat -= bar_len              # bar 1 = the first bar holding sound

    # bars
    bars: List[Bar] = []
    start, number = downbeat, 1
    while start < duration:
        per_beat = [_window_mean(rms, t, start + k * beat, start + (k + 1) * beat)
                    for k in range(num)]
        bars.append(Bar(number, start, float(np.mean(per_beat)), per_beat,
                        _window_max(bass_onset, t, start, 0.03, 0.06)))
        start += bar_len
        number += 1
    audible = [b.loud for b in bars if b.loud > 0]
    ref = float(np.percentile(audible, 90)) if audible else 1.0
    for b in bars:
        b.loud /= ref
        b.beats = [x / ref for x in b.beats]
    kick_ref = float(np.percentile([b.kick for b in bars], 90)) or 1.0
    for b in bars:
        b.kick /= kick_ref

    # builds: a crescendo through the bar into a clearly louder downbeat. A
    # song fading up out of silence is not a build, hence the floor.
    for b, nxt in zip(bars, bars[1:]):
        rising = b.beats[-1] >= tuning.build_rise * max(b.beats[0], 1e-6) \
            and sum(b.beats[num // 2:]) > sum(b.beats[:num // 2])
        lands = nxt.beats[0] >= 1.2 * b.loud and nxt.beats[0] >= 0.75 * b.beats[-1]
        b.build = bool(rising and lands and b.loud >= BUILD_MIN)

    for b in bars:
        b.level = (SILENT if b.loud < SILENT_MAX else QUIET if b.loud < tuning.quiet_max
                   else HIGH if b.loud >= tuning.high_min else MID)
    sound = [b for b in bars if b.level != SILENT]
    first_sound = sound[0].number if sound else 1
    last_sound = sound[-1].number if sound else len(bars)
    for b in bars:
        if b.number == first_sound:
            b.build = False

    _energies(bars, a, spec, bar_len)
    _voices(bars, sr, mono, bar_len)
    sections = _sections(bars, spec, beat, num, first_sound, last_sound, tuning)
    for s in sections:
        s.hits = _hits(bars, a, s, beat, num)
    impacts = _impacts(bars, a, beat, bar_len)
    accents = _accents(bars, sections, a, impacts)
    end_of_sound = bars[last_sound].start if last_sound < len(bars) else duration
    weak = _weak_grid(bars, t, strength, beat, bar_len, fit, first_sound, last_sound)
    warnings += [f"the grid does not fit bars {first}-{last} (a tempo change, or no beat "
                 f"to follow): cues there may miss the beat" for first, last in weak]
    song = SongMap(duration, bpm_fit, beat, num, downbeat, fit, tracker, bars,
                   sections, impacts, accents, end_of_sound, warnings)
    song.weak_grid = weak
    song.climax = _climax(sections, tuning.climax_share)
    song.outro = _outro(sections, song.climax)
    song.ending, song.final_hit, song.cut = _ending(a, bars, end_of_sound, bar_len)
    song.stops = _stops(a, beat, sections)
    song.key = _key(spec, sound_from, sound_to)
    return song


def _weak_grid(bars, t, strength, beat, bar_len, fit, first, last) -> List[Tuple[int, int]]:
    """The (first, last) bars of stretches of 16+ bars where, over any 8
    bars, the beats carry under twice the onset strength of the off-beats —
    in a song the grid otherwise fits. A tempo change (a medley, a rubato
    passage) one grid cannot follow, or a passage with no beat to follow at
    all. Blips of up to 4 bars inside such a stretch don't end it."""
    if fit < 2.5:
        return []
    span = [b for b in bars if first <= b.number <= last]
    if len(span) < 16:
        return []
    hits = np.array([float(np.interp(np.arange(b.start, b.start + bar_len - 1e-9, beat),
                                     t, strength).sum()) for b in span])
    misses = np.array([float(np.interp(np.arange(b.start, b.start + bar_len - 1e-9, beat)
                                       + beat / 2, t, strength).sum()) for b in span])
    ratio = _running_mean(hits, 8) / np.maximum(_running_mean(misses, 8), 1e-9)
    weak = list(ratio < 2.0)
    for i in range(len(weak)):                   # bridge short blips
        if not weak[i] and any(weak[max(0, i - 4):i]) and any(weak[i + 1:i + 5]):
            weak[i] = True
    out, run = [], []
    for b, w in zip(span + [None], weak + [False]):
        if w:
            run.append(b.number)
            continue
        if len(run) >= 16:
            out.append((run[0], run[-1]))
        run = []
    return out


# ---------------------------------------------------------------------------
# Spectral helpers
# ---------------------------------------------------------------------------
def _unit_rows(x: np.ndarray) -> np.ndarray:
    return x / (np.linalg.norm(x, axis=1, keepdims=True) + 1e-9)


def _span_chroma(spec: dict, starts, length: float) -> np.ndarray:
    """The chroma of each span [start, start + length), unit length."""
    t, chroma = spec["t"], spec["chroma"]
    rows = []
    for a in starts:
        m = (t >= a) & (t < a + length)
        rows.append(chroma[:, m].mean(axis=1) if m.any() else np.zeros(12))
    return _unit_rows(np.array(rows))


def _chord_change(chords: np.ndarray) -> np.ndarray:
    """How far the chords of the two beats from each beat on are from those
    of the two before it."""
    change = np.zeros(len(chords))
    for k in range(2, len(chords) - 1):
        before, after = chords[k - 2:k].mean(axis=0), chords[k:k + 2].mean(axis=0)
        change[k] = 1 - before @ after / (np.linalg.norm(before) * np.linalg.norm(after) + 1e-9)
    return change


def _span_mean(spec: dict, key: str, start: float, end: float) -> np.ndarray:
    t, x = spec["t"], spec[key]
    m = (t >= start) & (t < end)
    return x[..., m].mean(axis=-1) if m.any() else np.zeros(x.shape[:-1])


def _rank(values) -> np.ndarray:
    """Each value's place among the others, 0 (lowest) to 1 (highest)."""
    values = np.asarray(values, dtype=float)
    if len(values) < 2:
        return np.full(len(values), 0.5)
    return np.argsort(np.argsort(values)) / (len(values) - 1)


def _energies(bars: List[Bar], a: dict, spec: dict, bar_len: float):
    """Each sounding bar's energy against the song's other bars: its
    loudness, how many onsets it holds, how bright and how heavy in the bass
    it is. Compressed masters keep a verse almost as loud as a chorus; the
    drums and the brightness still tell them apart."""
    sound = [b for b in bars if b.level != SILENT]
    if not sound:
        return
    onsets = np.array([ot for ot, _s in a["onsets"]])
    loud = _rank([b.loud for b in sound])
    density = _rank([((onsets >= b.start) & (onsets < b.start + bar_len)).sum() for b in sound])
    bright = _rank([float(_span_mean(spec, "centroid", b.start, b.start + bar_len)) for b in sound])
    low = _rank([_window_mean(a["bass"], a["t"], b.start, b.start + bar_len) for b in sound])
    for b, x in zip(sound, 0.45 * loud + 0.25 * density + 0.15 * bright + 0.15 * low):
        b.energy = float(x)


def _voices(bars: List[Bar], sr: int, mono: np.ndarray, bar_len: float):
    """Each sounding bar's voice: how surely a voice sings in it, 0-1 —
    VOICE_WEIGHTS on its glide, tilt and level, each against the song's
    other bars. A song whose partials never glide is instrumental, and no
    bar of it sung."""
    sound = [b for b in bars if b.level != SILENT]
    if len(sound) < 2:
        return
    v = voice(sr, mono, bar_len)
    means = np.array([[_window_mean(v[k], v["t"], b.start, b.start + bar_len)
                       for k in ("glide", "tilt", "level")] for b in sound])
    if np.percentile(means[:, 0], 90) < GLIDE_FLOOR:
        return
    score = (means - means.mean(axis=0)) / (means.std(axis=0) + 1e-9) @ VOICE_WEIGHTS[:3]
    for b, x in zip(sound, 1 / (1 + np.exp(-(score + VOICE_WEIGHTS[3])))):
        b.voice = float(x)


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------
def _zscore(x: np.ndarray) -> np.ndarray:
    return (x - x.mean()) / (x.std() + 1e-9)


def _novelty(similar: np.ndarray, span: int) -> np.ndarray:
    """Foote novelty: how unlike the `span` bars before each bar are to the
    `span` from it, while each side stays like itself."""
    n = len(similar)
    r = np.arange(-span, span) + 0.5
    g = np.exp(-0.5 * (r / (span * 0.6)) ** 2)
    kernel = np.outer(g, g) * np.outer(np.sign(r), np.sign(r))
    padded = np.pad(similar, span, mode="edge")
    out = np.array([(padded[i:i + 2 * span, i:i + 2 * span] * kernel).sum() for i in range(n)])
    return np.maximum(out, 0)


def _grid_phase(score: np.ndarray) -> int:
    """Where the song's 8-bar lines fall (an index into `score`, mod 8): of
    the four 4-bar phases the one whose bars score highest, then of its two
    8-bar phases the higher."""
    four = max(range(min(4, len(score))), key=lambda p: score[p::4].mean())
    return max((p for p in (four, four + 4) if p < len(score)), key=lambda p: score[p::8].mean())


def _phrases(score: np.ndarray, forced: set, cost: float = SEGMENT_COST) -> List[int]:
    """The cut points (indices into `score`, each starting a phrase) that
    maximise the novelty cut through plus the phrase-length and grid
    rewards, less `cost` per phrase. A phrase runs two bars at least —
    but the last, which may be one bar holding the song's final hit."""
    n = len(score)
    if n < 4:
        return []
    phase = _grid_phase(score)

    def length_bonus(k):
        return PHRASE8 if k % 8 == 0 else PHRASE4 if k % 4 == 0 else 0.0 if k % 2 == 0 else ODD_PHRASE

    def grid_bonus(j):
        k = (j - phase) % 8
        return GRID8 if k == 0 else GRID4 if k == 4 else 0.0 if k % 2 == 0 else OFF_GRID

    best = {0: (0.0, None)}
    for j in range(2, n + 1):
        gain = (score[j] + grid_bonus(j) if j < n else 0.0) + (100.0 if j in forced else 0.0)
        options = [(best[i][0] + gain + length_bonus(j - i) - cost, i)
                   for i in range(0, j - 1 if j < n else j) if i in best]
        if options:
            best[j] = max(options)
    cuts, j = [], n
    while j:
        j = best[j][1]
        if j:
            cuts.append(j)
    return sorted(cuts)


def _materials(segments, timbre_sim, chord_sim, sung, same: float) -> List[str]:
    """A letter per segment, shared by segments that play the same music:
    their chords line up bar for bar (at the best whole-bar offset), they
    sound alike, and a voice sings both or neither (`sung`, per segment) —
    an intro playing the verse's riff is not the verse. Average-linkage
    clustering, cut where two groups are no longer `same` deviations more
    alike than the song's average."""
    n = len(segments)
    sim = np.full((n, n), np.nan)
    for i in range(n):
        for j in range(i + 1, n):
            (a0, a1), (b0, b1) = segments[i], segments[j]
            if a1 - a0 < b1 - b0:
                (a0, a1), (b0, b1) = (b0, b1), (a0, a1)
            length = b1 - b0
            chords = max(float(np.mean([chord_sim[a0 + off + k, b0 + k] for k in range(length)]))
                         for off in range(a1 - a0 - length + 1))
            sim[i, j] = sim[j, i] = chords + float(timbre_sim[a0:a1, b0:b1].mean())
    # a one-bar segment (the song's last hit) is no music to compare: it
    # would drag the average down and let unlike phrases pass for alike
    whole = [i for i, (a, b) in enumerate(segments) if b - a >= 2]
    pairs = np.array([sim[i, j] for i in whole for j in whole if i < j])
    cut = pairs.mean() + same * pairs.std() if len(pairs) > 1 else np.inf
    for i in range(n):
        for j in range(n):
            if sung[i] != sung[j]:
                sim[i, j] = -np.inf
    groups = [{i} for i in range(n)]
    while len(groups) > 1:
        link, x, y = max((float(np.mean([sim[i, j] for i in groups[x] for j in groups[y]])), x, y)
                         for x in range(len(groups)) for y in range(x + 1, len(groups)))
        if link < cut:
            break
        groups[x] |= groups.pop(y)
    groups.sort(key=min)
    letter = {i: _LETTERS[k % len(_LETTERS)] for k, g in enumerate(groups) for i in g}
    return [letter[i] for i in range(n)]


def _sections(bars: List[Bar], spec: dict, beat: float, num: int,
              first: int, last: int, tuning: Tuning = Tuning()) -> List[Section]:
    """Sections between the first and the last bar with sound.

    The song is cut into phrases where its timbre and chords change and
    where a voice starts or stops singing (and always where a build lands:
    what it lands on is new), preferring 4- and 8-bar phrases on the song's
    grid. Phrases are clustered into materials, and a run of one material at
    one loudness level, with no build inside, is one section. A song with a
    voice names its sections by where it sings (_name_sung), an instrumental
    one by loudness (_name)."""
    span = [b for b in bars if first <= b.number <= last]
    bar_len = beat * num
    if not span:
        return []
    timbre = np.array([_span_mean(spec, "timbre", b.start, b.start + bar_len) for b in span])
    timbre = _unit_rows((timbre - timbre.mean(axis=0)) / (timbre.std(axis=0) + 1e-9))
    chords = _unit_rows(np.array([_span_chroma(spec, [b.start + k * beat for k in range(num)],
                                               beat).ravel() for b in span]))
    timbre_sim, chord_sim = timbre @ timbre.T, chords @ chords.T
    score = (_novelty(_zscore(timbre_sim), NOVELTY_SPAN)
             + 0.5 * _novelty(_zscore(chord_sim), NOVELTY_SPAN))
    score = _zscore(score)
    loud = [b.level >= MID for b in span]
    for i in range(1, len(span)):
        if loud[i] != loud[i - 1] and all(x == loud[i] for x in loud[i:i + MIN_SECTION_BARS]):
            score[i] += TURN_BONUS
    sung = np.array([b.voice for b in span])
    for i in range(1, len(span)):
        score[i] += VOICE_TURN * abs(sung[i:i + 4].mean() - sung[max(0, i - 4):i].mean())
    forced = {i + 1 for i, b in enumerate(span[:-1]) if b.build}
    edges = [0] + _phrases(score, forced, tuning.segment_cost) + [len(span)]
    segments = list(zip(edges, edges[1:]))
    voiced = [bool(sung[i:j].mean() >= 0.5) for i, j in segments]
    letters = _materials(segments, timbre_sim, chord_sim, voiced,
                         SAME_MATERIAL_SUNG if any(voiced) else SAME_MATERIAL)

    def level_of(group):
        body = [b.loud for b in group if not b.build] or [b.loud for b in group]
        x = float(np.median(body))
        return QUIET if x < tuning.quiet_max else HIGH if x >= tuning.high_min else MID

    sections: List[Section] = []
    for (i, j), letter, is_sung in zip(segments, letters, voiced):
        group = span[i:j]
        level = level_of(group)
        prev = sections[-1] if sections else None
        if (prev is not None and prev.material == letter and prev.level == level
                and not span[i - 1].build):
            prev.last, prev.end = group[-1].number, group[-1].start + bar_len
            prev.phrases.append(group[0].number)
            continue
        sections.append(Section("", level, group[0].number, group[-1].number,
                                group[0].start, group[-1].start + bar_len, letter,
                                sung=is_sung, phrases=[group[0].number]))
    by_number = {b.number: b for b in span}
    for s in sections:
        s.energy = float(np.mean([by_number[n].energy for n in range(s.first, s.last + 1)]))
    if any(voiced):
        _name_sung(sections)
    else:
        _name(sections)
    return sections


def _name_sung(sections: List[Section]):
    """What a voice sings is a VERSE or a CHORUS, what it does not an INTRO
    before the singing starts, an OUTRO after it ends, and between an INST
    when loud and a BREAK when quiet. The CHORUS is the sung material that
    comes back with the most energy and fills the most of the sung time (any
    sung material, when none comes back); a sung material heard once
    between choruses is a BRIDGE."""
    sung = [k for k, s in enumerate(sections) if s.sung]
    count: Dict[str, int] = {}
    energy: Dict[str, List[float]] = {}
    length: Dict[str, int] = {}
    for k in sung:
        s = sections[k]
        count[s.material] = count.get(s.material, 0) + 1
        energy.setdefault(s.material, []).append(s.energy)
        length[s.material] = length.get(s.material, 0) + s.last - s.first + 1
    total = sum(length.values())
    repeated = [m for m in count if count[m] >= 2] or list(count)
    chorus = max(repeated, key=lambda m: np.mean(energy[m]) + length[m] / total)
    choruses = [k for k in sung if sections[k].material == chorus]
    for k, s in enumerate(sections):
        if not s.sung:
            s.part = ("INTRO" if k < sung[0] else "OUTRO" if k > sung[-1] else
                      "INST" if s.level >= MID else "BREAK")
        elif s.material == chorus:
            s.part = "CHORUS"
        elif count[s.material] == 1 and choruses[0] < k < choruses[-1]:
            s.part = "BRIDGE"
        else:
            s.part = "VERSE"


def _name(sections: List[Section]):
    """A quiet section is the INTRO before any loud one, the OUTRO after the
    last, a BREAK between. Of the loud ones, the CHORUS is the material that
    comes back with the most energy and fills the most of the loud time
    (any material, when none comes back); the opening material played loud
    later is an INST; a one-off loud section
    after the last chorus that ends the song is a loud OUTRO, one between
    choruses a BRIDGE; anything else is a VERSE."""
    loud = [k for k, s in enumerate(sections) if s.level >= MID]
    count: Dict[str, int] = {}
    energy: Dict[str, List[float]] = {}
    length: Dict[str, int] = {}
    for k in loud:
        s = sections[k]
        count[s.material] = count.get(s.material, 0) + 1
        energy.setdefault(s.material, []).append(s.energy)
        length[s.material] = length.get(s.material, 0) + s.last - s.first + 1
    total = sum(length.values()) or 1
    repeated = [m for m in count if count[m] >= 2] or list(count)
    # a pre-chorus can peak above the chorus; it does not fill as much of the song
    chorus = (max(repeated, key=lambda m: np.mean(energy[m]) + length[m] / total)
              if repeated else None)
    choruses = [k for k in loud if sections[k].material == chorus]
    opener = sections[0].material if sections else None
    for k, s in enumerate(sections):
        if s.level < MID:
            s.part = ("INTRO" if not loud or k < loud[0] else
                      "OUTRO" if k > loud[-1] else "BREAK")
        elif s.material == chorus:
            s.part = "CHORUS"
        elif s.material == opener and k > 0:
            s.part = "INST"
        elif count[s.material] == 1 and k == loud[-1] and k > choruses[-1]:
            s.part = "OUTRO"
        elif count[s.material] == 1 and choruses[0] < k < choruses[-1]:
            s.part = "BRIDGE"
        else:
            s.part = "VERSE"


def _climax(sections: List[Section], share: float = CLIMAX_SHARE) -> Optional[int]:
    """The section the song builds to: the last loud one of 4 bars or more,
    unless it falls short of `share` of the strongest loud section's
    energy — then the last one that does not."""
    loud = [k for k, s in enumerate(sections) if s.level >= MID and s.last - s.first >= 3]
    loud = loud or [k for k, s in enumerate(sections) if s.level >= MID]
    if not loud:
        return None
    top = max(sections[k].energy for k in loud)
    return max(k for k in loud if sections[k].energy >= share * top)


def _outro(sections: List[Section], climax: Optional[int]) -> Optional[int]:
    """The climax, when it is a chorus the song ends on: no more than
    TAIL_BARS bars after it, all of them quiet — its last hit, or the ring
    of it. A song that plays on after its climax has no such outro."""
    if climax is None or sections[climax].part != "CHORUS":
        return None
    tail = sections[climax + 1:]
    if any(s.level >= MID for s in tail) or sum(s.last - s.first + 1 for s in tail) > TAIL_BARS:
        return None
    return climax


def _hits(bars: List[Bar], a: dict, sec: Section, beat: float, num: int) -> List[float]:
    """The beats (1-based, in eighths) a section's snare and cymbals lean
    on: the eighths of its bar whose high-band attack, averaged over the
    section, stands well above the rest."""
    if sec.level < MID:
        return []
    t = a["t"]
    attack = np.maximum(np.diff(a["high"], prepend=a["high"][0]), 0)
    slots = 2 * num
    profile = np.zeros(slots)
    for b in bars:
        if sec.first <= b.number <= sec.last and not b.build:
            for k in range(slots):
                profile[k] += _window_max(attack, t, b.start + k * beat / 2, 0.03, 0.05)
    if profile.max() <= 0:
        return []
    strong = [k for k in range(slots)
              if profile[k] >= 1.3 * profile.mean() and profile[k] >= 0.6 * profile.max()]
    return [1 + k / 2 for k in strong] if len(strong) < slots // 2 + 1 else []


def _ending(a: dict, bars: List[Bar], end_of_sound: float, bar_len: float):
    """``(kind, last hit, cut)``: how the song stops, the time of its last
    hit and when the sound is gone. A song still loud in its last two bars
    with sound ends on a hit: a CUT when the sound dies within CUT_TAIL of
    it, else a RING. One already quiet by then FADEs. Onset strengths are
    relative to the local level, so a reverb tail scores like a hit: the hit
    is where the loudness last jumps, the cut where it drops under a tenth
    of the song's loud level."""
    t, rms = a["t"], _running_mean(a["rms"], 4)
    audible = rms[rms > 1e-4]
    ref = float(np.percentile(audible, 90)) if len(audible) else 1.0
    sound = [b for b in bars if b.level != SILENT]
    loud = [b for b in sound if b.level >= MID]
    if not loud or loud[-1].number < sound[-1].number - 2:
        return "fade", None, end_of_sound
    strong = np.nonzero(rms >= 0.5 * ref)[0]
    if not len(strong):
        return "fade", None, end_of_sound
    loud_end = float(t[strong[-1]])
    gone = np.nonzero((t > loud_end) & (rms < 0.1 * ref))[0]
    cut = float(t[gone[0]]) if len(gone) else end_of_sound
    beat = bar_len / len(bars[0].beats)
    near = [ot for ot, _s in a["onsets"] if loud_end - 2 * beat <= ot <= loud_end + 0.05]
    hit = max(near, key=lambda ot: _rise(rms, t, ot), default=loud_end)
    return ("cut" if cut - hit <= CUT_TAIL else "ring"), float(hit), cut


def _stops(a: dict, beat: float, sections: List[Section]) -> List[Tuple[float, float]]:
    """Where the band drops out for a moment inside the music: the sound
    under STOP_DEPTH of the song's loud level for STOP_BEATS beats (and
    STOP_MIN seconds) or more, with loud music within a second either side.
    Ends on the onset that brings it back."""
    if not sections:
        return []
    t, rms = a["t"], _running_mean(a["rms"], 3)
    audible = rms[rms > 1e-4]
    ref = float(np.percentile(audible, 90)) if len(audible) else 1.0
    quiet = rms < STOP_DEPTH * ref
    reach = int(a["fps"])
    onsets = [ot for ot, _s in a["onsets"]]
    stops: List[Tuple[float, float]] = []
    i, n = 0, len(t)
    while i < n:
        if not quiet[i]:
            i += 1
            continue
        j = i
        while j < n and quiet[j]:
            j += 1
        start, end = float(t[i]), float(t[min(j, n - 1)])
        loud_around = (i and rms[max(0, i - reach):i].max() >= 0.5 * ref
                       and j < n and rms[j:j + reach].max() >= 0.5 * ref)
        if (loud_around and end - start >= max(STOP_MIN, STOP_BEATS * beat)
                and sections[0].start < start and end < sections[-1].end):
            back = [ot for ot in onsets if end - 0.1 <= ot <= end + 0.1]
            stops.append((start, back[0] if back else end))
        i = j
    return stops


def _key(spec: dict, start: float, end: float) -> str:
    """The key whose Krumhansl-Kessler profile the song's chroma follows best."""
    m = (spec["t"] >= start) & (spec["t"] <= end)
    if not m.any():
        return ""
    chroma = _unit_rows(spec["chroma"][:, m].T).sum(axis=0)
    best = max(((np.corrcoef(chroma, np.roll(profile, tonic))[0, 1], tonic, mode)
                for mode, profile in (("major", _MAJOR), ("minor", _MINOR))
                for tonic in range(12)))
    return f"{_NOTES[best[1]]} {best[2]}"


def _rise(env, t, at, span=0.05):
    """How much the loudness envelope climbs across `at`."""
    return float(np.interp(at + span, t, env) - np.interp(at - span, t, env))


def _impacts(bars: List[Bar], a: dict, beat: float, bar_len: float):
    """The hit each build lands on, plus any sudden loudness jump: the instant
    the envelope climbs fastest, pinned to an onset when one is that close."""
    t, env = a["t"], a["env"]
    onsets = [ot for ot, _s in a["onsets"]]
    scan = np.arange(0, t[-1], 0.01)
    climb = np.interp(scan + 0.05, t, env) - np.interp(scan - 0.05, t, env)

    def sharpest(lo, hi):
        m = (scan >= lo) & (scan < hi)
        if not m.any():
            return None
        at = float(scan[m][int(np.argmax(climb[m]))])
        near = [o for o in onsets if abs(o - at) <= 0.03]
        return min(near, key=lambda o: abs(o - at)) if near else at

    found = []
    for b, nxt in zip(bars, bars[1:]):
        if b.build:
            at = sharpest(b.start + bar_len - 1.5 * beat, nxt.start + beat)
            if at is not None:
                found.append((at, _rise(env, t, at)))
    # standalone: twice as loud as the second before, and loud for the song —
    # a soft kick after a pause doubles the level too
    loud = float(np.percentile(env[env > 0.01], 90)) if (env > 0.01).any() else 1.0
    for ot, _s in a["onsets"]:
        before = _window_mean(env, t, ot - 1.5, ot - 0.05)
        after = _window_mean(env, t, ot, ot + 0.3)
        if before > 0 and after / before >= 2.2 and after >= 0.6 * loud:
            found.append((ot, _rise(env, t, ot)))
    found.sort()
    impacts: List[Tuple[float, float]] = []
    for at, strength in found:
        if impacts and at - impacts[-1][0] < bar_len:
            if strength > impacts[-1][1]:
                impacts[-1] = (at, strength)
            continue
        impacts.append((at, strength))
    return impacts


def _accents(bars, sections, a, impacts):
    """The strongest onsets of quiet passages, at least half a second apart,
    clear of impacts and of build bars, two per bar at most."""
    quiet = [s for s in sections if s.level == QUIET]
    if not quiet:
        return []
    inside = [(ot, s) for ot, s in a["onsets"]
              if any(q.start <= ot < q.end for q in quiet)]
    if not inside:
        return []
    floor = max(5.0, float(np.percentile([s for _t, s in inside], 90)))
    picked: List[Tuple[float, float]] = []
    bar_len = bars[1].start - bars[0].start if len(bars) > 1 else 2.0
    for ot, s in sorted(inside, key=lambda x: -x[1]):
        if s < floor:
            break
        bar = bars[max(0, min(len(bars) - 1, int((ot - bars[0].start) // bar_len)))]
        if bar.build or any(abs(ot - it) < 0.6 for it, _ in impacts):
            continue
        if any(abs(ot - p) < 0.5 for p, _ in picked):
            continue
        if sum(1 for p, _ in picked if bar.start <= p < bar.start + bar_len) >= 2:
            continue
        picked.append((ot, s))
    return sorted(picked)


def describe(song: SongMap, bars: bool = False) -> str:
    lines = [f"tempo {song.bpm:.2f} BPM (live tracker: "
             f"{song.tracker_bpm and round(song.tracker_bpm, 1)}), grid fit "
             f"{song.fit:.2f}x, {song.beats_per_bar} beats/bar, first downbeat "
             f"{song.first_downbeat:.3f}s, {len(song.bars)} bars, sound until "
             f"{song.end_of_sound:.2f}s"]
    lines.append(f"key {song.key or '?'}, ending: {song.ending}"
                 + (f" (last hit {song.final_hit:.2f}s, sound gone {song.cut:.2f}s)"
                    if song.final_hit is not None else ""))
    for k, (part, s) in enumerate(song.parts()):
        builds = [b.number for b in song.bars if b.build and s.first <= b.number <= s.last]
        lines.append(f"  bars {s.first:3d}-{s.last:3d}  {s.start:7.2f}s  {part:7s} "
                     f"{s.material} {LEVEL_NAMES[s.level]:5s} energy {s.energy:.2f}"
                     + ("  sung" if s.sung else "")
                     + (f"  phrases {s.phrases[1:]}" if len(s.phrases) > 1 else "")
                     + (f"  hits {s.hits}" if s.hits else "")
                     + (f"  builds {builds}" if builds else "")
                     + ("  <- CLIMAX" if k == song.climax else ""))
    lines += [f"  WARNING {w}" for w in song.warnings]
    lines.append("  impacts " + ", ".join(f"{at:.2f}s" for at, _ in song.impacts))
    lines.append("  stops " + ", ".join(f"{a:.2f}-{b:.2f}s" for a, b in song.stops))
    lines.append("  accents " + ", ".join(f"{at:.2f}s" for at, _ in song.accents))
    if bars:
        for b in song.bars:
            lines.append(f"    bar {b.number:3d} {b.start:7.2f}s  loud {b.loud:4.2f}  "
                         f"{LEVEL_NAMES[b.level]:6s} kick {b.kick:4.2f}  beats "
                         + " ".join(f"{x:4.2f}" for x in b.beats)
                         + f"  energy {b.energy:.2f}  voice {b.voice:.2f}"
                         + ("  BUILD" if b.build else ""))
    return "\n".join(lines)
