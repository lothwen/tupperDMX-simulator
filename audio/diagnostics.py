# ABOUTME: TimingMonitor — the click's flight recorder: per-beat scheduling error,
# ABOUTME: audio-feed health and sink underruns, summarised once per song.
"""What the app knows about its own timing, so a glitch at a show leaves a trace.

A live click is only trustworthy if a deviation is *recorded* rather than felt
and forgotten, so both halves of the beat's journey report here:

- `record_beat()` from the metronome — how far off its own schedule each beep
  fired; `record_dropped()` for beats a stall made unplayable.
- `record_feed()` from the audio engine — the interval between buffer top-ups
  and whether the sink ran dry (an underrun IS the dropout you heard).

Two rules shape the implementation. **Aggregates, not history**: the feed runs
~66 times a second for the length of a show, so it contributes counters, never
a row. **Warnings are rate-limited**: the thread whose starvation is being
reported is the same thread that would write the log line, and on an SBC that
line goes to flash — a warning per feed would deepen the very stall it
describes.

What this does NOT measure: where the beep lands in the OUTPUT once queued.
A one-shot is mixed into the next 15 ms block and that block is written behind
whatever the sink still holds, so the audible beat carries a further variable
delay (measured on a healthy desktop: 52-92 ms). That is a property of the
push-buffer design, invisible from here, and only a sample-scheduled click
removes it. The error reported below is the SCHEDULING error — the half this
app currently controls — and the summary says so.

Nothing here is Qt, and nothing here touches the sink: the engine reads the
device state and passes the verdict in.
"""

import os
import sys
import threading
import time
import traceback
from typing import Dict, List, Optional

from tupperlogger import get_logger

# A feed gap this long has not underrun yet (the buffer holds ~80 ms) but is
# the early warning that something is taking the Qt thread away from the audio.
LATE_FEED_MS = 40.0

# Bound on the per-beat errors kept for percentiles: an hour of the fastest
# tempo the app plays. Past it the counters still track, the percentiles just
# stop sampling — a metronome left running overnight must not grow a list.
MAX_BEATS = 20000

# Minimum spacing between warnings from one monitor.
WARN_INTERVAL_S = 1.0


def _percentile(ordered: List[float], pct: float) -> float:
    """`pct` percentile of an already-sorted list (0.0 when empty)."""
    if not ordered:
        return 0.0
    index = int(round((pct / 100.0) * (len(ordered) - 1)))
    return ordered[max(0, min(len(ordered) - 1, index))]


class TimingMonitor:
    """Collects click and audio-feed timing for one playback pass.

    `reset()` arms it at the top of a song; `summary()` is the one line worth
    reading the morning after a show."""

    def __init__(self):
        self.logger = get_logger("audio.timing")
        # The audio thread records feeds while the Qt thread records beats and
        # reads the summary, so every field below is shared. The lock is only
        # ever held for arithmetic — never across a log write, which on the
        # rack means a flash write and would hand the audio thread a stall to
        # wait on.
        self._lock = threading.Lock()
        self._last_warn = 0.0
        self._suppressed = 0
        self.reset()

    def reset(self) -> None:
        """Start a fresh pass. Counters only — the warning rate limit carries
        over, so a box that is stalling through a whole set cannot get a fresh
        budget of log writes at every song boundary."""
        with self._lock:
            self._beat_errors_ms: List[float] = []
            self._beats = 0
            self._beats_dropped = 0
            self._worst_beat_ms = 0.0
            self._feeds = 0
            self._late_feeds = 0
            self._underruns = 0
            self._worst_gap_ms = 0.0
            self._last_feed: Optional[float] = None
            # Smallest lead any beat was scheduled with. With a lookahead
            # scheduler this, not the beat's error, is the health metric: the
            # beep lands exactly where placed, so what can still go wrong is
            # placing it too late. Approaching zero means the lookahead is
            # smaller than the stalls the machine is producing.
            self._min_lead_s: Optional[float] = None

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------
    def record_beat(self, error_sec: float) -> None:
        """A click beep was queued `error_sec` after the beat it belongs to
        (negative = early)."""
        error_ms = error_sec * 1000.0
        with self._lock:
            self._beats += 1
            if len(self._beat_errors_ms) < MAX_BEATS:
                self._beat_errors_ms.append(error_ms)
            self._worst_beat_ms = max(self._worst_beat_ms, abs(error_ms))

    def record_lead(self, lead_sec: float) -> None:
        """A beat was scheduled `lead_sec` ahead of the output playhead."""
        with self._lock:
            if self._min_lead_s is None or lead_sec < self._min_lead_s:
                self._min_lead_s = lead_sec

    def record_dropped(self, count: int, wait_sec: float) -> None:
        """`count` beats were too late to play; the click resumes in
        `wait_sec` on the next beat of the grid."""
        if count <= 0:
            return
        with self._lock:
            self._beats_dropped += count
        self._warn(f"Click recovered from a stall: {count} beat(s) dropped, "
                   f"back on the grid in {wait_sec * 1000:.0f} ms")

    def record_feed(self, underrun: bool, fill_sec: float) -> None:
        """One audio buffer top-up: `underrun` is the sink's own verdict that
        it ran dry, `fill_sec` what was still queued ahead of this block."""
        now = time.monotonic()
        with self._lock:
            gap_ms = (0.0 if self._last_feed is None
                      else (now - self._last_feed) * 1000.0)
            self._last_feed = now
            self._feeds += 1
            self._worst_gap_ms = max(self._worst_gap_ms, gap_ms)
            if underrun:
                self._underruns += 1
            elif gap_ms > LATE_FEED_MS:
                self._late_feeds += 1

        if underrun:
            self._warn(f"Audio underrun — the sink ran dry ({gap_ms:.0f} ms "
                       f"since the last feed): a dropout was audible")
        elif gap_ms > LATE_FEED_MS:
            self._warn(f"Audio feed {gap_ms:.0f} ms late (buffer was down to "
                       f"{fill_sec * 1000:.0f} ms): close to a dropout")

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def stats(self) -> Dict[str, float]:
        """The pass so far, as numbers (what the tests assert on)."""
        with self._lock:
            errors = list(self._beat_errors_ms)
            snapshot = (self._beats, self._beats_dropped, self._worst_beat_ms,
                        self._feeds, self._late_feeds, self._underruns,
                        self._worst_gap_ms,
                        -1.0 if self._min_lead_s is None else self._min_lead_s)
        ordered = sorted(abs(e) for e in errors)
        return {
            "beats": snapshot[0],
            "beats_dropped": snapshot[1],
            "beat_p50_ms": _percentile(ordered, 50),
            "beat_p99_ms": _percentile(ordered, 99),
            "beat_max_ms": snapshot[2],
            "feeds": snapshot[3],
            "late_feeds": snapshot[4],
            "underruns": snapshot[5],
            "worst_feed_gap_ms": snapshot[6],
            "min_lead_ms": snapshot[7] * 1000.0,
        }

    def had_activity(self) -> bool:
        """Whether this pass recorded anything worth a summary line.

        What makes a pass reportable ONCE: stopping the transport runs the
        same path twice (stop, then shutdown's cleanup), and a second
        identical line reads as a second song."""
        with self._lock:
            return bool(self._beats or self._underruns or self._late_feeds)

    def summary(self) -> str:
        """One line per song: what a live operator needs to trust the click.

        "sched err" is how far each beep missed its own schedule, not how far
        the audience heard it move — see the module docstring."""
        s = self.stats()
        if not s["beats"] and not s["feeds"]:
            return "Timing: nothing played"
        dropped = (f", {int(s['beats_dropped'])} DROPPED"
                   if s["beats_dropped"] else "")
        lead = ("" if s["min_lead_ms"] < 0
                else f", min lead {s['min_lead_ms']:.0f} ms")
        return (
            f"Timing: click {int(s['beats'])} beats, late "
            f"p50 {s['beat_p50_ms']:.1f} p99 {s['beat_p99_ms']:.1f} "
            f"max {s['beat_max_ms']:.1f} ms{dropped}{lead} | "
            f"audio {int(s['underruns'])} underruns, "
            f"{int(s['late_feeds'])} late feeds, "
            f"worst gap {s['worst_feed_gap_ms']:.0f} ms "
            f"in {int(s['feeds'])} feeds")

    def last_feed(self) -> Optional[float]:
        """Monotonic time of the last buffer top-up — the Qt thread's pulse,
        which is what StallAttributor watches from outside."""
        with self._lock:
            return self._last_feed

    # ------------------------------------------------------------------
    def _warn(self, message: str) -> None:
        """Rate-limited warning. The bookkeeping happens under the lock, the
        log write outside it: a write is a flush (flash, on the rack) and the
        audio thread must never queue behind one."""
        now = time.monotonic()
        with self._lock:
            if now - self._last_warn < WARN_INTERVAL_S:
                self._suppressed += 1
                return
            if self._suppressed:
                message = f"{message} (+{self._suppressed} more suppressed)"
                self._suppressed = 0
            self._last_warn = now
        self.logger.warning(message)


# ----------------------------------------------------------------------
# Stall attribution
# ----------------------------------------------------------------------
# Opt-in: the sampler is a Python thread waking 100x a second, which itself
# competes for the GIL it is trying to measure. Off unless asked for.
STALL_TRACE_ENV = "TUPPERDMX_STALL_TRACE"

# A Qt thread that has not fed the sink in this long is stalled, not busy.
STALL_THRESHOLD_MS = 40.0
SAMPLE_INTERVAL_S = 0.010
# Frames of the Qt thread's Python stack to report, innermost last.
STACK_DEPTH = 5
# Mean lateness per sampler wake-up that means we were queueing behind a GIL
# holder rather than simply sharing an idle machine. Python hands the GIL over
# every `sys.setswitchinterval()` (5 ms by default), so a thread competing with
# a GIL-holding loop slips by milliseconds per wake, while one whose rival is
# inside C++ slips by a fraction of one. Measured 2.4 vs 0.15 ms.
GIL_CONTENTION_MS = 1.0


class StallAttributor:
    """Names what is blocking the Qt thread, by watching it from another one.

    The TimingMonitor can say the audio feed ran late; it cannot say WHY,
    because it only runs when the thread it would blame lets it. This samples
    from outside instead: a daemon thread wakes every 10 ms, notices when the
    feed's heartbeat has gone stale, and captures the Qt thread's Python stack
    while it is still stuck.

    It also answers the question that decides the FIX. Two stalls look alike
    from the audio's side but are not:

    - the Qt thread is inside C++ (a vsync-blocking buffer swap, a QPainter
      run, GL calls) — the GIL is released there, so this sampler keeps its own
      10 ms rhythm throughout. Moving the audio feed to its own thread would
      then keep it fed right through the stall.
    - the Qt thread is running PYTHON and holding the GIL — this sampler is
      starved too, and its own wake-ups slip by the same order. No amount of
      threading rescues the audio from that; the offending code has to get
      cheaper or move.

    So each report carries how much the sampler ITSELF lost during the stall.
    That ratio is the recommendation."""

    def __init__(self, heartbeat, threshold_ms: float = STALL_THRESHOLD_MS):
        self.logger = get_logger("audio.stall")
        self._heartbeat = heartbeat
        self._threshold_s = threshold_ms / 1000.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.stalls = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="stall-attributor",
                                        daemon=True)
        self._thread.start()
        self.logger.info(
            f"Stall attribution on (threshold {self._threshold_s * 1000:.0f} ms) "
            "— watching the Qt thread from outside")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    # ------------------------------------------------------------------
    def _run(self) -> None:
        main_id = threading.main_thread().ident
        in_stall = False
        stall_started = 0.0
        sampler_lost = 0.0      # how much WE slipped while it was stuck
        sampler_wakes = 0
        stacks: List[str] = []

        expected = time.monotonic() + SAMPLE_INTERVAL_S
        while not self._stop.wait(SAMPLE_INTERVAL_S):
            now = time.monotonic()
            # Our own lateness IS the GIL measurement: we asked for 10 ms.
            own_late = max(0.0, now - expected)
            expected = now + SAMPLE_INTERVAL_S

            last = self._heartbeat()
            if last is None:
                continue
            stale = now - last

            if stale > self._threshold_s:
                if not in_stall:
                    in_stall, stall_started = True, last
                    sampler_lost, sampler_wakes, stacks = 0.0, 0, []
                sampler_lost += own_late
                sampler_wakes += 1
                if len(stacks) < 3:
                    captured = self._capture(main_id)
                    if captured and captured not in stacks:
                        stacks.append(captured)
            elif in_stall:
                in_stall = False
                self._report(last - stall_started, sampler_lost,
                             sampler_wakes, stacks)

    @staticmethod
    def _capture(thread_id) -> str:
        """The Qt thread's Python stack right now, innermost frames last."""
        frame = sys._current_frames().get(thread_id)
        if frame is None:
            return ""
        entries = traceback.extract_stack(frame)[-STACK_DEPTH:]
        return " <- ".join(
            f"{os.path.basename(e.filename)}:{e.lineno} {e.name}"
            for e in reversed(entries))

    def _report(self, duration_s: float, sampler_lost_s: float,
                sampler_wakes: int, stacks: List[str]) -> None:
        self.stalls += 1
        duration_ms = duration_s * 1000
        lost_ms = sampler_lost_s * 1000
        per_wake = lost_ms / max(1, sampler_wakes)
        # Our own slip per wake-up is the GIL measurement: keeping our rhythm
        # means the GIL stayed free, which means an audio thread of our own
        # would have kept feeding straight through this.
        gil_held = per_wake > GIL_CONTENTION_MS
        verdict = ("GIL HELD — Python code blocked it, a separate audio thread "
                   "would stall too"
                   if gil_held else
                   "GIL free — it blocked in C++, a separate audio thread would "
                   "have kept feeding")
        self.logger.warning(
            f"Qt thread blocked {duration_ms:.0f} ms ({verdict}; sampler lost "
            f"{lost_ms:.0f} ms over {sampler_wakes} wakes = "
            f"{per_wake:.2f} ms each)")
        for stack in stacks:
            self.logger.warning(f"    stuck at: {stack}")


def maybe_start_attributor(heartbeat) -> Optional[StallAttributor]:
    """Start stall attribution iff the environment asks for it."""
    if not os.environ.get(STALL_TRACE_ENV):
        return None
    attributor = StallAttributor(heartbeat)
    attributor.start()
    return attributor
