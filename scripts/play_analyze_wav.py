#!/usr/bin/env python3
"""Play a WAV file while printing live audio analysis to the console.

Standalone R&D prototype for the Fase 6 auto-lights direction (hand-rolled
numpy/scipy DSP, no app integration whatsoever):

  * energy            total + 3-band IIR filterbank (bass/mid/high) with
                      asymmetric attack/release envelope followers
  * onsets            spectral flux + adaptive median threshold peak picking
  * tempo (BPM)       autocorrelation of the onset envelope, log-BPM prior
  * beat phase        phase-locked oscillator nudged by onsets (prediction)
  * brightness        spectral centroid
  * sections          QUIET/GROOVE/PEAK state machine on short-vs-long-term
                      energy ratio, with hysteresis + dwell

Playback goes through the first external player found (pw-play / paplay /
aplay / ffplay); the analysis loop is paced against the wall clock so the
console events line up with what you hear.

Usage:
    uv run python scripts/play_analyze_wav.py path/to/song.wav
    uv run python scripts/play_analyze_wav.py song.wav --no-audio   # silent, full speed
"""

import argparse
import shutil
import subprocess
import time
from collections import deque

import numpy as np
from scipy import signal
from scipy.io import wavfile

FRAME = 1024            # STFT window (~23 ms @ 44.1 kHz)
HOP = 512               # analysis hop (~12 ms @ 44.1 kHz)
BPM_MIN, BPM_MAX = 40.0, 300.0   # same clamp BpmState uses
STATUS_EVERY = 1.0      # seconds between status lines

GRAY, BOLD, CYAN, YELLOW, RESET = "\033[90m", "\033[1m", "\033[36m", "\033[33m", "\033[0m"
BLOCKS = " ▁▂▃▄▅▆▇█"

PLAYERS = [
    ("pw-play", []),
    ("paplay", []),
    ("aplay", ["-q"]),
    ("ffplay", ["-nodisp", "-autoexit", "-loglevel", "quiet"]),
]


def meter(x):
    x = min(max(x, 0.0), 1.0)
    return BLOCKS[round(x * (len(BLOCKS) - 1))]


def load_wav(path):
    sr, data = wavfile.read(path)
    if data.dtype.kind == "i":
        data = data.astype(np.float32) / np.iinfo(data.dtype).max
    elif data.dtype.kind == "u":            # 8-bit WAV is unsigned
        data = (data.astype(np.float32) - 128.0) / 128.0
    else:
        data = data.astype(np.float32)
    mono = data.mean(axis=1) if data.ndim == 2 else data
    peak = float(np.abs(mono).max())
    if peak > 0:                            # normalize so meters/thresholds compare across files
        mono = mono / peak
    return sr, mono


def start_player(path):
    for name, args in PLAYERS:
        exe = shutil.which(name)
        if exe:
            proc = subprocess.Popen([exe, *args, path],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return proc, name
    return None, None


class EnvFollower:
    """RMS smoother with fast attack / slow release (compressor-sidechain trick)."""

    def __init__(self, fps, attack=0.01, release=0.3):
        self.a_att = 1.0 - np.exp(-1.0 / (attack * fps))
        self.a_rel = 1.0 - np.exp(-1.0 / (release * fps))
        self.y = 0.0

    def process(self, x):
        a = self.a_att if x > self.y else self.a_rel
        self.y += a * (x - self.y)
        return self.y


class OnsetDetector:
    """Spectral flux + adaptive median threshold (Dixon-style peak picking)."""

    def __init__(self, fps):
        self.window = np.hanning(FRAME).astype(np.float32)
        self.prev_mag = None
        self.hist = deque(maxlen=int(round(fps)))   # ~1 s of flux for the median
        self.f1 = self.f2 = 0.0                     # last two flux values
        self.t1 = 0.0
        self.last_onset = -10.0

    def process(self, frame, t):
        """Returns (flux, mag, onset) — onset is None or (time, strength)."""
        mag = np.abs(np.fft.rfft(frame * self.window))
        if self.prev_mag is None:
            flux = 0.0
        else:
            flux = float(np.maximum(mag - self.prev_mag, 0.0).sum()) / (FRAME / 2)
        self.prev_mag = mag

        # a peak is confirmed one hop late: f1 must top both neighbours
        onset = None
        med = float(np.median(self.hist)) if self.hist else 0.0
        thr = max(1.5 * med + 1e-4, 0.1 * max(self.hist, default=0.0))
        if (self.f1 > thr and self.f1 > self.f2 and self.f1 >= flux
                and self.t1 - self.last_onset > 0.06):
            self.last_onset = self.t1
            onset = (self.t1, self.f1 / thr)

        self.hist.append(flux)
        self.f2, self.f1, self.t1 = self.f1, flux, t
        return flux, mag, onset


class TempoEstimator:
    """Autocorrelation of the onset-strength envelope with a gentle log-BPM prior."""

    def __init__(self, fps):
        self.fps = fps
        self.env = deque(maxlen=int(8 * fps))       # ~8 s rolling window

    def add(self, flux):
        self.env.append(flux)

    def estimate(self):
        if len(self.env) < int(4 * self.fps):
            return None
        env = np.asarray(self.env, dtype=np.float32)
        env -= env.mean()
        ac = np.correlate(env, env, "full")[len(env) - 1:]
        if ac[0] <= 0:
            return None
        ac /= ac[0]
        lo = max(2, int(self.fps * 60.0 / BPM_MAX))
        hi = min(len(ac) - 2, int(self.fps * 60.0 / BPM_MIN))
        if hi <= lo:
            return None
        bpms = 60.0 * self.fps / np.arange(lo, hi + 1)
        prior = np.exp(-0.5 * (np.log2(bpms / 120.0)) ** 2)   # sigma = 1 octave around 120
        k = int(np.argmax(ac[lo:hi + 1] * prior)) + lo
        a, b, c = ac[k - 1], ac[k], ac[k + 1]                 # parabolic lag refinement
        denom = a - 2 * b + c
        shift = 0.5 * (a - c) / denom if abs(denom) > 1e-12 else 0.0
        lag = k + float(np.clip(shift, -1.0, 1.0))
        conf = float(np.clip(b, 0.0, 1.0))                    # normalized autocorr at the peak
        return 60.0 * self.fps / lag, conf


class BeatPLL:
    """Phase-locked beat oscillator: period from the tempo estimate, phase nudged
    by onsets so predicted beats converge onto the audible grid."""

    def __init__(self):
        self.period = None
        self.next_beat = None
        self.conf = 0.0
        self.count = 0

    def set_tempo(self, bpm, conf, t):
        period = 60.0 / bpm
        if self.period is None:
            self.period = period
            self.next_beat = t + period
        elif abs(period - self.period) / self.period > 0.08:
            self.period = period                     # tempo jump: snap, keep phase
        else:
            self.period = 0.7 * self.period + 0.3 * period
        self.conf = conf

    def on_onset(self, t):
        if self.period is None:
            return
        err = (t - self.next_beat + self.period / 2) % self.period - self.period / 2
        if abs(err) < 0.3 * self.period:
            self.next_beat += (0.1 + 0.3 * self.conf) * err

    def tick(self, t):
        beats = []
        if self.period is None:
            return beats
        while self.next_beat <= t:
            self.count += 1
            beats.append((self.next_beat, self.count))
            self.next_beat += self.period
        return beats


class SectionTracker:
    """QUIET / GROOVE / PEAK on short(1 s) vs long(12 s) energy, hysteresis + dwell."""

    DWELL = 0.7     # seconds a candidate state must persist before switching
    WARMUP = 3.0    # no transitions until the long-term average means something

    def __init__(self, fps):
        self.a_short = 1.0 - np.exp(-1.0 / (1.0 * fps))
        self.a_long = 1.0 - np.exp(-1.0 / (12.0 * fps))
        self.short = self.long = 0.0
        self.state = "QUIET"
        self.cand = None
        self.cand_since = 0.0
        self.initialized = False

    def _classify(self, ratio):
        if self.short < 0.02:
            return "QUIET"
        if self.state == "PEAK":                     # harder to leave than to enter
            return "PEAK" if ratio > 1.1 else "GROOVE"
        if ratio > 1.35 and self.short > 0.08:
            return "PEAK"
        if ratio < 0.55:
            return "QUIET"
        return "GROOVE"

    def process(self, rms, t):
        if not self.initialized:
            self.short = self.long = rms
            self.initialized = True
        self.short += self.a_short * (rms - self.short)
        self.long += self.a_long * (rms - self.long)
        ratio = self.short / (self.long + 1e-9)
        cand = self._classify(ratio)
        change = None
        if cand == self.state:
            self.cand = None
        elif cand != self.cand:
            self.cand, self.cand_since = cand, t
        elif t - self.cand_since >= self.DWELL and t >= self.WARMUP:
            change = (self.state, cand, ratio)
            self.state, self.cand = cand, None
        return change


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("wav", help="path to a WAV file")
    ap.add_argument("--no-audio", action="store_true",
                    help="skip playback and run the analysis at full speed")
    args = ap.parse_args()

    sr, mono = load_wav(args.wav)
    fps = sr / HOP
    print(f"{args.wav}: {sr} Hz, {len(mono) / sr:.1f} s  "
          f"(frame {FRAME}, hop {HOP}, {fps:.1f} analysis fps)")

    # 3-band causal IIR filterbank over the whole signal (identical to streamed sosfilt)
    sos = [signal.butter(4, 120, "lowpass", fs=sr, output="sos"),
           signal.butter(4, [120, 2000], "bandpass", fs=sr, output="sos"),
           signal.butter(4, 2000, "highpass", fs=sr, output="sos")]
    bands = [signal.sosfilt(s, mono) for s in sos]

    onset_det = OnsetDetector(fps)
    tempo_est = TempoEstimator(fps)
    pll = BeatPLL()
    sections = SectionTracker(fps)
    env_total = EnvFollower(fps)
    env_bands = [EnvFollower(fps) for _ in bands]
    freqs = np.fft.rfftfreq(FRAME, 1.0 / sr)
    centroid_sm = 0.0

    player = None
    if not args.no_audio:
        player, name = start_player(args.wav)
        if player:
            print(f"playing via {name}\n")
        else:
            print("warning: no player found (pw-play/paplay/aplay/ffplay) — "
                  "running silent at real-time pace; use --no-audio for full speed\n")

    bpm = conf = None
    shown_bpm = None
    last_status = last_tempo = 0.0
    n_onsets = n_beats = 0
    start = time.perf_counter()
    try:
        for i in range((len(mono) - FRAME) // HOP + 1):
            pos = i * HOP
            t = (pos + FRAME / 2) / sr

            if not args.no_audio:
                delay = t - (time.perf_counter() - start)
                if delay > 0:
                    time.sleep(delay)

            frame = mono[pos:pos + FRAME]
            flux, mag, onset = onset_det.process(frame, t)
            tempo_est.add(flux)

            rms = float(np.sqrt(np.mean(frame[:HOP] ** 2)))
            e_total = env_total.process(rms)
            e_bands = [f.process(float(np.sqrt(np.mean(b[pos:pos + HOP] ** 2))))
                       for f, b in zip(env_bands, bands)]

            msum = float(mag.sum())
            if msum > 1e-9:
                centroid_sm += 0.05 * (float((freqs * mag).sum()) / msum - centroid_sm)

            change = sections.process(rms, t)

            if onset:
                ot, strength = onset
                n_onsets += 1
                pll.on_onset(ot)
                print(f"{GRAY}{ot:7.2f}s  onset  {meter(strength / 8)}  x{strength:.1f}{RESET}")

            if t - last_tempo >= 0.5:
                last_tempo = t
                est = tempo_est.estimate()
                if est:
                    bpm, conf = est
                    pll.set_tempo(bpm, conf, t)
                    if shown_bpm is None or abs(bpm - shown_bpm) > 2.0:
                        was = f"  [was {shown_bpm:.1f}]" if shown_bpm is not None else ""
                        print(f"{YELLOW}{t:7.2f}s  TEMPO  {bpm:.1f} BPM (conf {conf:.2f}){was}{RESET}")
                        shown_bpm = bpm

            for bt, count in pll.tick(t):
                n_beats += 1
                print(f"{BOLD}{bt:7.2f}s  BEAT {(count - 1) % 4 + 1}  "
                      f"♩ {60.0 / pll.period:6.1f} BPM  conf {pll.conf:.2f}{RESET}")

            if change:
                old, new, ratio = change
                print(f"{CYAN}{t:7.2f}s  ── SECTION {old} → {new}  (energy ratio {ratio:.2f}) ──{RESET}")

            if t - last_status >= STATUS_EVERY:
                last_status = t
                bpm_s = f"{bpm:6.1f} BPM ({conf:.2f})" if bpm else "    --"
                print(f"{t:7.2f}s  status  E {meter(e_total * 3)}  "
                      f"bass {meter(e_bands[0] * 3)} mid {meter(e_bands[1] * 3)} "
                      f"high {meter(e_bands[2] * 3)}  bright {centroid_sm / 1000:4.1f} kHz  "
                      f"{bpm_s}  {sections.state}")
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        if player and player.poll() is None:
            player.terminate()

    tempo_s = f"{bpm:.1f} BPM (conf {conf:.2f})" if bpm else "undetected"
    print(f"\ndone: {n_onsets} onsets, {n_beats} beats, tempo {tempo_s}, "
          f"final section {sections.state}")


if __name__ == "__main__":
    main()
