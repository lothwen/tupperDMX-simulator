"""
Characterization test: click timing and its flight recorder.

Covers what Tier 1 changed — the metronome's precise timer, its monotonic
clock, the caller-supplied phase anchor and its refusal to replay beats it was
too late to play — plus the TimingMonitor that records all of it. Fakes only:
no audio device, no GL.

Run with:
    uv run python tests/test_click_timing.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from audio.diagnostics import LATE_FEED_MS, TimingMonitor
from core.tempo_map import TempoMap
from test_count_in import FakeEngine


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _controller(bpm=120):
    from app.metronome import Metronome
    engine = FakeEngine()
    controller = Metronome(engine)
    controller.set_bpm(bpm)
    return controller, engine


# ----------------------------------------------------------------------
# The monitor
# ----------------------------------------------------------------------
def test_monitor_beat_statistics():
    print("test_monitor_beat_statistics")
    monitor = TimingMonitor()
    for error_ms in (1.0, -2.0, 3.0, 40.0):
        monitor.record_beat(error_ms / 1000.0)
    stats = monitor.stats()
    _assert(stats["beats"] == 4, "every beat counted")
    _assert(stats["beat_max_ms"] == 40.0,
            "the worst beat is the ABSOLUTE error, sign discarded")
    _assert(stats["beat_p99_ms"] == 40.0, "p99 reaches the worst beat")
    _assert(stats["beats_dropped"] == 0, "nothing dropped without being told")


def test_monitor_counts_underruns_and_late_feeds():
    print("test_monitor_counts_underruns_and_late_feeds")
    monitor = TimingMonitor()
    monitor.record_feed(underrun=False, fill_sec=0.08)
    time.sleep((LATE_FEED_MS + 10) / 1000.0)
    monitor.record_feed(underrun=False, fill_sec=0.01)
    monitor.record_feed(underrun=True, fill_sec=0.0)
    stats = monitor.stats()
    _assert(stats["feeds"] == 3, "every feed counted")
    _assert(stats["late_feeds"] == 1, "the gap over the threshold was late")
    _assert(stats["underruns"] == 1, "the sink's dry verdict was recorded")
    _assert(stats["worst_feed_gap_ms"] > LATE_FEED_MS,
            "the worst gap survives into the summary")
    _assert("underruns" in monitor.summary(), "the summary says so")


def test_monitor_reset_clears_the_pass():
    print("test_monitor_reset_clears_the_pass")
    monitor = TimingMonitor()
    monitor.record_beat(0.05)
    monitor.record_dropped(2, 0.1)
    monitor.reset()
    stats = monitor.stats()
    _assert(stats["beats"] == 0 and stats["beats_dropped"] == 0,
            "reset arms a fresh pass")
    _assert(monitor.summary() == "Timing: nothing played",
            "a pass with nothing in it says so rather than printing zeros")


def test_monitor_reports_a_pass_once():
    print("test_monitor_reports_a_pass_once")
    monitor = TimingMonitor()
    _assert(not monitor.had_activity(), "a fresh pass has nothing to report")
    monitor.record_beat(0.001)
    _assert(monitor.had_activity(), "a played beat makes the pass reportable")
    monitor.reset()
    _assert(not monitor.had_activity(),
            "…and once reported it stays quiet: stopping the transport runs "
            "the same path twice, and a repeat line reads as a second song")
    monitor.record_feed(underrun=True, fill_sec=0.0)
    _assert(monitor.had_activity(),
            "an underrun alone is still worth a line, click or no click")


def test_monitor_rate_limits_its_warnings():
    print("test_monitor_rate_limits_its_warnings")
    monitor = TimingMonitor()
    emitted = []
    monitor.logger.warning = lambda msg: emitted.append(msg)
    for _ in range(50):
        monitor.record_feed(underrun=True, fill_sec=0.0)
    _assert(len(emitted) == 1,
            "50 underruns in a burst cost ONE log write, not 50 — the stalled "
            "thread must not be handed the writing of its own obituary")
    _assert(monitor.stats()["underruns"] == 50,
            "…while every one of them is still counted")


# ----------------------------------------------------------------------
# The metronome: a scheduler, not a timer
# ----------------------------------------------------------------------
RATE = 44100


def _beats_at(engine):
    """(frame, downbeat) of every click queued so far."""
    return list(engine.scheduled)


def test_metronome_timer_is_precise():
    print("test_metronome_timer_is_precise")
    controller, _ = _controller()
    _assert(controller.metronome_timer.timerType() == Qt.TimerType.PreciseTimer,
            "the pump keeps a tidy cadence, not Qt's coalescing default")


def test_metronome_phases_on_the_caller_anchor():
    print("test_metronome_phases_on_the_caller_anchor")
    controller, engine = _controller(bpm=120)
    # The transport was at t=1.1 a quarter second ago (a slow seek tick).
    controller.start_metronome(1.1, time.monotonic() - 0.25)
    # 0.25 s of tick means the transport is really at 1.35 now, so the next
    # beat (t=1.5) is 0.15 s away — not the 0.4 s a naive reading would give.
    first = engine.scheduled[0][0]
    _assert(abs(first - 0.15 * RATE) < 0.01 * RATE,
            f"the grid is phased on the anchor, not on 'now' "
            f"({first / RATE:.3f}s, expected 0.150s)")


def test_the_count_in_keeps_its_opening_downbeat():
    print("test_the_count_in_keeps_its_opening_downbeat")
    controller, engine = _controller(bpm=120)
    numbers = []
    controller.beat_scheduled.connect(lambda n, t, f: numbers.append(n))
    # A realistic start: the transport's position was read a few ms before the
    # anchor was taken, so the beat AT that position is now marginally behind
    # the playhead. It must still sound — it is the count-in's downbeat.
    controller.start_metronome(-4.0, time.monotonic() - 0.004)
    _assert(numbers and numbers[0] == 1,
            f"the count-in opens on beat 1, not {numbers[:1]} — any lag at "
            "all between reading the position and anchoring would otherwise "
            "skip it")
    _assert(engine.clicks and engine.clicks[0] is True,
            "and it is sounded as a downbeat")


def test_beats_land_on_exact_frames():
    print("test_beats_land_on_exact_frames")
    controller, engine = _controller(bpm=137)   # a tempo with an ugly period
    frames = []
    controller.beat_scheduled.connect(lambda n, t, f: frames.append((t, f)))
    controller.start_metronome(0.0)
    for _ in range(40):
        engine.advance(0.1)
        controller._pump()
    worst = max(abs(f - round(t * RATE)) for t, f in frames)
    _assert(len(frames) > 8 and worst <= 1,
            f"{len(frames)} beats, none off its own sample by more than "
            f"{worst} — at 137 BPM the period is not a whole number of frames "
            "and it still does not accumulate")


def test_a_stall_shorter_than_the_lookahead_is_inaudible():
    print("test_a_stall_shorter_than_the_lookahead_is_inaudible")
    controller, engine = _controller(bpm=120)
    controller.start_metronome(0.0)
    queued_before = len(engine.scheduled)

    # The Qt thread is blocked: the output runs on, nothing pumps. This is the
    # 129 ms the stage was measured blocking for, with margin.
    engine.advance(0.3)
    controller._pump()

    _assert(controller.monitor.stats()["beats_dropped"] == 0,
            "a 300 ms stall costs no beat at all — the beeps for that window "
            "were already in the buffer before it started")
    _assert(len(engine.scheduled) > queued_before,
            "and the pump simply carries on queueing")
    frames = [f for f, _d in engine.scheduled]
    gaps = {round(frames[i + 1] - frames[i]) for i in range(len(frames) - 1)}
    _assert(gaps == {RATE // 2},
            f"every beat still exactly half a second apart ({gaps})")


def test_a_stall_longer_than_the_lookahead_drops_rather_than_crowds():
    print("test_a_stall_longer_than_the_lookahead_drops_rather_than_crowds")
    controller, engine = _controller(bpm=120)
    controller.start_metronome(0.0)

    # Longer than LOOKAHEAD_S: beats past the queued window never got placed,
    # and the playhead has gone by them.
    engine.advance(1.6)
    controller._pump()

    dropped = controller.monitor.stats()["beats_dropped"]
    _assert(dropped >= 1,
            f"beats the output has already passed are dropped ({dropped})")
    rendered = engine.frames_rendered()
    _assert(all(f >= rendered for f, _d in engine.scheduled
                if f >= rendered) and
            not [f for f, _d in engine.scheduled
                 if rendered - RATE < f < rendered],
            "nothing is crowded in behind the playhead to catch up")
    # The grid is intact: what is queued is still on the original half-second.
    ahead = sorted(f for f, _d in engine.scheduled if f >= rendered)
    _assert(all(f % (RATE // 2) == 0 for f in ahead),
            "the beats that do sound are still on the grid it started on")


def test_beat_occurred_follows_the_output_not_the_queue():
    print("test_beat_occurred_follows_the_output_not_the_queue")
    controller, engine = _controller(bpm=120)
    heard = []
    controller.beat_occurred.connect(heard.append)
    controller.start_metronome(0.0)
    _assert(heard == [1],
            "only the beat at the playhead has been reached so far")
    queued = len(engine.scheduled)
    _assert(queued >= 1 and len(heard) <= queued,
            "beats are queued ahead of being heard, never the other way round")

    # Run normally: the pump keeps up, so no beat is missed.
    for _ in range(10):
        engine.advance(0.1)
        controller._pump()
    _assert(heard == [1, 2, 3],
            f"the indicator reports beats as the output reaches them ({heard})")
    _assert(controller.monitor.stats()["beats_dropped"] == 0,
            "and nothing was dropped while the pump kept up")


def test_stopping_keeps_a_beep_that_is_already_sounding():
    print("test_stopping_keeps_a_beep_that_is_already_sounding")
    from audio.mixer import BUS_CLICK, Mixer
    import numpy as np
    mixer = Mixer()
    clip = np.ones(1000, dtype=np.float32)
    mixer.schedule(BUS_CLICK, clip, 0)       # starts now
    mixer.schedule(BUS_CLICK, clip, 40000)   # queued well ahead
    mixer.render(200)                        # the first one begins sounding
    mixer.clear_scheduled(BUS_CLICK)
    block = mixer.render(200)
    _assert(float(np.abs(block).max()) > 0.0,
            "the beep already leaving the speaker finishes — cutting it is "
            "how a clean stop turns into a pop")
    mixer.render(60000)
    _assert(float(np.abs(mixer.render(200)).max()) == 0.0,
            "…while the beat queued ahead of the playhead is gone")


def test_monitor_tracks_the_scheduling_lead():
    print("test_monitor_tracks_the_scheduling_lead")
    controller, engine = _controller(bpm=120)
    controller.start_metronome(0.0)
    for _ in range(10):
        engine.advance(0.1)
        controller._pump()
    lead = controller.monitor.stats()["min_lead_ms"]
    _assert(0.0 < lead <= 500.0,
            f"the closest any beat came to its deadline is on the record "
            f"({lead:.0f} ms) — the health metric for a lookahead scheduler")
    _assert(lead > 300.0,
            f"and with the pump keeping up it stays near the full lookahead "
            f"({lead:.0f} ms of 500) — the opening beat, placed AT the "
            "playhead by design, is excluded so it cannot peg this at zero")
    _assert("min lead" in controller.monitor.summary(),
            "and it reaches the summary an operator reads")


# ----------------------------------------------------------------------
# Stall attribution
# ----------------------------------------------------------------------
def _attributor_says(duration_ms, lost_ms, wakes):
    from audio.diagnostics import StallAttributor
    attributor = StallAttributor(heartbeat=lambda: None)
    said = []
    attributor.logger.warning = lambda msg: said.append(msg)
    attributor._report(duration_ms / 1000.0, lost_ms / 1000.0, wakes, [])
    return said[0]


def test_attributor_clears_a_cpp_block():
    print("test_attributor_clears_a_cpp_block")
    # A vsync-blocking swap or a QPainter run: the GIL is released, so the
    # sampler keeps its rhythm (measured 0.11 ms per wake).
    said = _attributor_says(duration_ms=130, lost_ms=1, wakes=8)
    _assert("GIL free" in said,
            "a block that let the sampler run is named as C++ — which is the "
            "case a separate audio thread would sail straight through")


def test_attributor_convicts_a_gil_holder():
    print("test_attributor_convicts_a_gil_holder")
    # Python code holding the GIL hands it over every switch interval (5 ms by
    # default), so the sampler slips by milliseconds per wake.
    said = _attributor_says(duration_ms=124, lost_ms=26, wakes=5)
    _assert("GIL HELD" in said,
            "a block that starved the sampler too is named as Python — "
            "threading the audio would NOT rescue it")


def test_attributor_verdict_is_per_wake_not_per_stall():
    print("test_attributor_verdict_is_per_wake_not_per_stall")
    # The same 26 ms lost over a long stall is many cheap wakes, not
    # contention. Judging it as a fraction of the stall got this backwards.
    said = _attributor_says(duration_ms=124, lost_ms=26, wakes=200)
    _assert("GIL free" in said,
            "lateness is judged per wake-up, against the GIL switch interval, "
            "not as a share of the stall")


def test_attributor_captures_the_calling_stack():
    print("test_attributor_captures_the_calling_stack")
    import threading
    from audio.diagnostics import StallAttributor
    captured = StallAttributor._capture(threading.current_thread().ident)
    _assert("test_click_timing.py" in captured and "captures" in captured,
            f"the stack names where the thread actually is ({captured[:60]}…)")


def test_attributor_is_opt_in():
    print("test_attributor_is_opt_in")
    import os as _os
    from audio.diagnostics import STALL_TRACE_ENV, maybe_start_attributor
    previous = _os.environ.pop(STALL_TRACE_ENV, None)
    try:
        _assert(maybe_start_attributor(lambda: None) is None,
                "no sampler thread unless asked for — it competes for the very "
                "GIL it measures")
    finally:
        if previous is not None:
            _os.environ[STALL_TRACE_ENV] = previous


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
