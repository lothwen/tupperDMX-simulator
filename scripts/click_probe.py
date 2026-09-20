#!/usr/bin/env python3
# ABOUTME: Measures THIS machine's click timing — Qt timer accuracy, click-to-
# ABOUTME: output latency, sound-card clock drift and recovery from a stall.
"""What the click actually does on the box it is running on.

The app's own `TimingMonitor` records the half of the error it controls (when
each beat was scheduled). These probes measure the rest — the parts that belong
to the platform, not to the app — and they exist because the numbers differ per
machine: a desktop on PipeWire and a rack SBC on PulseAudio are not the same
audio stack, and the SBC is the one that plays the show.

    uv run python scripts/click_probe.py all          # on the dev box
    python3 scripts/click_probe.py all                # on the rack (buildroot)

`scripts/` is deliberately excluded from the rack image (buildroot/post-build.sh),
so copy this one file onto the box when you need it — it imports nothing the
running app does not already have, and resolves the app from its own location.

Probes (`all` runs every one):

  timer     Does this kernel honour a 1 ms timer? Schedules beats exactly as
            the metronome does, with Qt's coarse default and with PreciseTimer,
            across musical tempos. The app uses precise; this confirms the
            platform delivers it. Needs no sound card.

  latency   How long after the metronome decides a beat does the beep reach the
            output stream, and how much does that vary? This is the push-buffer
            jitter that Tier 1 does NOT fix — the number that says whether a
            sample-scheduled click (Tier 2) is worth building.

  clock     The sound card's sample clock against time.monotonic(), in ppm.
            The backing advances on the card's clock while the click is
            scheduled on the system's, so any difference is click-vs-track
            drift, hidden by the 150 ms resync deadband until it snaps. Measured
            -1 ppm on the dev box (PipeWire rate-matches); the SBC is the open
            question. Needs the longest run — a minute buys ~16 ppm resolution.

  stall     Blocks the Qt thread the way a samba copy or a flash write does,
            then checks the click recovered on the grid (no burst, no early
            beat) and that the underrun was detected and logged.

Silent by construction: the click bus is held at zero and nothing is loaded, so
these can be run on a rig that is patched and live without anyone hearing them.
"""

import argparse
import os
import statistics
import sys
import time
from pathlib import Path

# Offscreen before any Qt import, exactly as main.py --headless does.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import Qt, QTimer                                # noqa: E402
from PyQt6.QtGui import QGuiApplication                            # noqa: E402

from audio.engine import AudioEngine                               # noqa: E402
from audio.mixer import BUS_CLICK                                  # noqa: E402


def _app():
    """The same application class app/headless.py boots QtMultimedia under."""
    return QGuiApplication.instance() or QGuiApplication([sys.argv[0]])


def _engine(app):
    """A silent AudioEngine, or None when the box has no usable output."""
    engine = AudioEngine()
    if not engine.is_running():
        print("  no audio sink — this box has no usable output device")
        return None
    engine.set_bus_volume(BUS_CLICK, 0.0)
    worker = engine._worker
    buffer_ms = (worker.buffer_bytes / worker.bytes_per_frame
                 / engine.rate() * 1000)
    print(f"  sink: {engine.rate()} Hz, "
          f"{'float32' if worker.use_float else 'int16'}, "
          f"buffer {buffer_ms:.0f} ms  (feed on its own thread)")
    return engine


def _verdict(ok, good, bad):
    print(f"  => {'OK   ' if ok else 'WARN '} {good if ok else bad}")


# ----------------------------------------------------------------------
def probe_timer(args):
    """Beat scheduling accuracy, coarse (Qt's default) vs precise (the app's)."""
    print("\n== timer: does this kernel honour a precise timer? ==")
    app = _app()

    def run(bpm, timer_type):
        interval = 60.0 / bpm
        timer = QTimer()
        timer.setSingleShot(True)
        if timer_type is not None:
            timer.setTimerType(timer_type)
        state = {"n": 0, "target": time.monotonic() + interval, "err": []}

        def fire():
            state["err"].append((time.monotonic() - state["target"]) * 1000.0)
            state["n"] += 1
            if state["n"] >= args.beats:
                app.quit()
                return
            # Drift-anchored, exactly as AudioController schedules.
            state["target"] += interval
            timer.start(max(0, round((state["target"] - time.monotonic()) * 1000)))

        timer.timeout.connect(fire)
        timer.start(round(interval * 1000))
        app.exec()
        return state["err"]

    print(f"  {'BPM':>5} {'beat ms':>8} | {'coarse spread':>13} "
          f"| {'PRECISE spread':>14} {'precise worst':>13}")
    worst_precise = 0.0
    for bpm in (70, 90, 110, 130, 160):
        coarse = run(bpm, None)
        precise = run(bpm, Qt.TimerType.PreciseTimer)
        worst_precise = max(worst_precise, max(map(abs, precise)))
        print(f"  {bpm:>5} {60000 / bpm:8.1f} | {max(coarse) - min(coarse):11.1f}ms "
              f"| {max(precise) - min(precise):12.2f}ms "
              f"{max(map(abs, precise)):11.2f}ms")
    _verdict(worst_precise < 5.0,
             f"precise timers land within {worst_precise:.1f} ms — the click's "
             "schedule is trustworthy here",
             f"precise timers miss by up to {worst_precise:.1f} ms — this box "
             "cannot schedule a beat accurately; check the kernel timer "
             "resolution and what else is running")


# ----------------------------------------------------------------------
def probe_latency(args):
    """Buffer fill distribution: the jitter a click would inherit if it were
    still placed at whatever moment the Qt thread asked for it."""
    print("\n== latency: buffer fill the output rides on ==")
    app = _app()
    engine = _engine(app)
    if engine is None:
        return
    worker = engine._worker
    rate, bpf = engine.rate(), worker.bytes_per_frame
    fills = []

    # Sampled from the Qt thread: it only reads two integers, and the feed it
    # is measuring now runs elsewhere.
    sampler = QTimer()
    sampler.setInterval(5)
    sampler.timeout.connect(lambda: fills.append(
        max(0, worker.buffer_bytes - worker._sink.bytesFree()) / bpf / rate * 1000))
    sampler.start()
    QTimer.singleShot(int(args.seconds * 1000), app.quit)
    app.exec()
    sampler.stop()
    engine.cleanup()

    if not fills:
        print("  nothing sampled")
        return
    spread = max(fills) - min(fills)
    print(f"  samples: {len(fills)}   mean {statistics.mean(fills):.1f} ms   "
          f"min {min(fills):.1f}   max {max(fills):.1f}")
    print(f"  SPREAD {spread:.1f} ms")
    print("  (the click no longer inherits this — it is placed by frame — but")
    print("   this is the headroom a stall has to eat before a dropout)")
    _verdict(min(fills) > 10.0,
             f"the buffer never fell below {min(fills):.0f} ms",
             f"the buffer fell to {min(fills):.0f} ms — little headroom left "
             "before a stall becomes an audible dropout")


# ----------------------------------------------------------------------
def probe_clock(args):
    """The sound card's sample clock vs monotonic, in ppm."""
    print(f"\n== clock: sound-card sample clock vs monotonic "
          f"({args.seconds:.0f} s) ==")
    import numpy as np

    app = _app()
    engine = _engine(app)
    if engine is None:
        return
    worker = engine._worker
    rate, bpf = engine.rate(), worker.bytes_per_frame
    total = {"frames": 0}
    original = engine.mixer.render

    def render(frames):
        total["frames"] += frames
        return original(frames)

    engine.mixer.render = render

    # Frames CONSUMED = rendered minus what is still sitting in the buffer.
    # That fill oscillates by tens of ms between feeds, so two point samples
    # would report the oscillation rather than the clock (a 30 ms swing over a
    # 45 s window is 667 ppm of pure noise — it read -480 ppm that way). Many
    # samples and a least-squares slope average it out, and the residual says
    # how much to trust the answer.
    samples = []

    def sample():
        fill = max(0, worker.buffer_bytes - worker._sink.bytesFree()) / bpf
        samples.append((time.monotonic(), (total["frames"] - fill) / rate))

    sampler = QTimer()
    sampler.setInterval(100)
    sampler.timeout.connect(sample)
    QTimer.singleShot(3000, sampler.start)   # settle before measuring
    QTimer.singleShot(int(3000 + args.seconds * 1000), app.quit)
    app.exec()
    sampler.stop()
    engine.cleanup()

    if len(samples) < 20:
        print("  too few samples to fit")
        return
    t = np.array([s[0] for s in samples])
    consumed = np.array([s[1] for s in samples])
    t -= t[0]
    slope, intercept = np.polyfit(t, consumed, 1)
    residual = consumed - (slope * t + intercept)
    stderr = (np.sqrt(np.sum(residual ** 2) / (len(t) - 2))
              / np.sqrt(np.sum((t - t.mean()) ** 2)))
    ppm, ppm_err = (slope - 1.0) * 1e6, stderr * 1e6
    drift_4min = ppm * 1e-6 * 240 * 1000

    print(f"  {len(samples)} samples over {t[-1]:.1f} s, "
          f"buffer swing {residual.std() * 1000:.1f} ms rms")
    print(f"  device clock: {ppm:+.0f} +/- {ppm_err:.0f} ppm")
    print(f"  => click-vs-track drift over a 4-minute song: {drift_4min:+.0f} ms")
    if abs(ppm) > 3 * ppm_err and abs(drift_4min) >= 1.0:
        minutes = 150.0 / abs(ppm * 1e-6 * 1000) / 60
        print(f"  => the 150 ms resync deadband is reached after {minutes:.0f} "
              "min of playback, and the backing then JUMPS")
    if abs(ppm) <= 3 * ppm_err:
        _verdict(True,
                 f"no drift resolvable above the noise (+/-{ppm_err:.0f} ppm) — "
                 "the audio stack is rate-matching the card, so the click will "
                 "not walk off the track",
                 "")
    else:
        _verdict(abs(drift_4min) < 20.0,
                 f"{abs(drift_4min):.0f} ms per song — close enough that the "
                 "click will not audibly walk off the track",
                 f"{abs(drift_4min):.0f} ms per song of click-vs-track drift, "
                 "resolved well above the noise. This is what a sample-scheduled "
                 "click (Tier 2) removes; nothing in Tier 1 addresses it")


# ----------------------------------------------------------------------
def probe_stall(args):
    """Block the Qt thread, then check the click was untouched by it."""
    print(f"\n== stall: blocking the Qt thread for {args.stall_ms} ms ==")
    app = _app()
    engine = _engine(app)
    if engine is None:
        return
    from app.audio_controller import AudioController
    from audio.mixer import BUS_CLICK

    # Where each beep was PLACED in the output. With a scheduled click this is
    # the whole truth: the frame is when it sounds.
    placed = []
    original = engine.mixer.schedule

    def schedule(bus, samples, at_frame):
        if bus == BUS_CLICK:
            placed.append(at_frame)
        return original(bus, samples, at_frame)

    engine.mixer.schedule = schedule

    controller = AudioController(engine, 0.0)
    controller.set_bpm(120)
    engine.monitor.reset()
    controller.start_metronome(0.0)

    QTimer.singleShot(4000, lambda: time.sleep(args.stall_ms / 1000.0))
    QTimer.singleShot(9000, app.quit)
    app.exec()
    controller.cleanup()
    engine.cleanup()

    if len(placed) < 6:
        print("  too few beats measured")
        return
    rate = engine.rate()
    beat = round(0.5 * rate)
    gaps = [placed[i + 1] - placed[i] for i in range(len(placed) - 1)]
    # Every gap a whole number of beats IS the grid surviving; anything else
    # means a beep moved, which is the failure this design exists to prevent.
    off_grid = [g for g in gaps if g % beat != 0]
    print(f"  beats placed: {len(placed)}   "
          f"gaps (beats): {sorted({g / beat for g in gaps})}")
    print(f"  {engine.monitor.summary()}")
    _verdict(not off_grid,
             "every beep landed on the grid the click started on — the stall "
             "cost whole beats and never moved one",
             f"{len(off_grid)} gap(s) are not a whole number of beats: "
             f"{[g / beat for g in off_grid]}")


PROBES = {"timer": probe_timer, "latency": probe_latency,
          "clock": probe_clock, "stall": probe_stall}


def main():
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("probe", choices=list(PROBES) + ["all"])
    parser.add_argument("--seconds", type=float, default=60.0,
                        help="measurement window for latency/clock (default 60)")
    parser.add_argument("--beats", type=int, default=24,
                        help="beats per tempo for the timer probe (default 24)")
    parser.add_argument("--stall-ms", type=int, default=1500,
                        help="how long the stall probe blocks (default 1500)")
    args = parser.parse_args()

    names = list(PROBES) if args.probe == "all" else [args.probe]
    for name in names:
        PROBES[name](args)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
