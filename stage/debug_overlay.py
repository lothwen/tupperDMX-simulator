# ABOUTME: The stage's debug readout — frame rate, paint cost and what the GPU
# ABOUTME: light layer actually did, pinned to the stage image's bottom-right.
"""The stage debug readout.

Shown only while logging is at DEBUG, and that IS its switch: the level is
re-read on every refresh, so Settings ▸ Logging — or an edit to
`userprefs.json` on a headless rack box — turns the panel on and off live. It
carries no preference of its own, which is deliberate: one more window setting
to remember, persist and migrate buys nothing over the level that is already
there and already reloadable.

Two things it reports cannot be discovered any other way:

* **What one repaint costs the Qt thread.** That is the number the rest of the
  app lives on: a stage paint of 46–129 ms is what emptied the audio sink's
  buffer and put the feed on a `TimeCriticalPriority` thread of its own (see
  `audio/engine.py`), and it is what the scheduled click is built to survive.
  A stall shows up here as the worst paint in the window while the frame rate
  is still respectable.
* **Whether the GPU path is really running, and what it was given.** Every
  failure in `stage/gpu/` is soft by design — no context, an OpenGL ES
  context, a float framebuffer the driver won't give, a shader that won't
  compile, a draw that throws — and the stage carries on rendering on the
  raster path, which looks exactly the same from outside. The `draw` line is
  written only by a layer that is actually drawing, so its absence is that
  answer; which driver is behind it stays in the log, where
  `LightLayer._initialise` names it once.

The probe that measures those paints is installed only while the panel is
showing, so an app at INFO pays one `is None` test per frame and nothing else —
this measures the very thing it must not cost.

The readout is recomputed on its own timer rather than per frame, and only
repaints when its text actually changes. A still stage therefore settles at
"0 fps" and stops costing anything at all, which is the same invariant the fog
march's convergence countdown protects.
"""

import logging
import time
from collections import deque

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QLabel

#: How long a window the frame rate and the worst paint are measured over. Long
#: enough for a number that doesn't flicker, short enough that a stall is gone
#: from it a moment after it stops happening.
WINDOW_S = 2.0

#: How often the text is recomputed — and, with it, how quickly a log level
#: changed in Settings shows or hides the panel.
REFRESH_MS = 500


#: Every count and every duration is written at a FIXED width. The panel is
#: anchored by its right edge and sized to its own text, so a number growing a
#: digit — a rig going from 9 primitives to 324, a paint from 9.9 ms to 103.5 —
#: would widen the block and shift the whole readout sideways as it ran.
_MS_WIDTH = 5
_COUNT_WIDTH = 4
_SETTLE_WIDTH = len("settling 9")


def _ms(seconds) -> str:
    return ("–" if seconds is None
            else f"{seconds * 1000.0:.1f}").rjust(_MS_WIDTH)


class StageDebugOverlay(QLabel):
    """A read-only panel over the stage. Owns nothing but its own timer."""

    def __init__(self, controller):
        super().__init__()
        self._controller = controller
        self._view = controller.get_stage_view()
        # (ended_at, paint_seconds) for the frames inside WINDOW_S.
        self._frames: deque = deque()
        # The root level is what `setup_logging` sets, and every component
        # logger inherits it — so this asks the same question Settings answers.
        self._logger = logging.getLogger("stage")
        self.setObjectName("StageDebugOverlay")
        self.setTextFormat(Qt.TextFormat.PlainText)
        # Flush with the stage's right edge, which is the one the panel is
        # pinned by: a line that is shorter than the widest grows leftwards
        # instead of leaving a ragged margin against the corner.
        self.setAlignment(Qt.AlignmentFlag.AlignRight
                          | Qt.AlignmentFlag.AlignVCenter)
        # It sits over the stage image, where fixtures are dragged in edit
        # mode: it must never be the thing the mouse finds.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    # -- the DEBUG gate ------------------------------------------------
    def refresh(self) -> None:
        """Re-read the log level, and the frame if it earns one."""
        wanted = self._logger.isEnabledFor(logging.DEBUG)
        if wanted == self.isHidden():
            self._set_active(wanted)
        if not wanted:
            return
        report = self._report()
        if report == self.text():
            return              # a still stage repaints nothing
        self.setText(report)
        # Bottom-anchored, so the panel's own height decides where it starts:
        # it has to be the size it will be drawn at before it is placed.
        self.adjustSize()
        self._view.reposition_overlays()

    def _set_active(self, active: bool) -> None:
        self.setVisible(active)
        self._view.set_frame_probe(self._on_frame if active else None)
        if not active:
            self._frames.clear()

    # -- what it measures ----------------------------------------------
    def _on_frame(self, started: float, ended: float) -> None:
        self._frames.append((ended, ended - started))
        self._trim(ended)

    def _trim(self, now: float) -> None:
        frames = self._frames
        cutoff = now - WINDOW_S
        while frames and frames[0][0] < cutoff:
            frames.popleft()

    def _rates(self):
        """(fps, last paint, worst paint) over the window, trimmed to NOW.

        Trimmed here and not only as frames arrive, because a stage that has
        stopped repainting sends none: without this the panel would keep
        quoting the rate of whatever happened before it went still.
        """
        self._trim(time.perf_counter())
        if not self._frames:
            return 0.0, None, None
        return (len(self._frames) / WINDOW_S, self._frames[-1][1],
                max(paint for _, paint in self._frames))

    # -- what it says --------------------------------------------------
    def _report(self) -> str:
        fps, last, worst = self._rates()
        lines = [f"fps {fps:5.1f}   paint {_ms(last)} / {_ms(worst)} ms"]
        lines.extend(self._gpu_lines())
        return "\n".join(lines)

    def _gpu_lines(self) -> list:
        """What the GPU drew, or nothing at all.

        Nothing is the answer for every state but a layer that is up and
        drawing — no layer, one still initialising, one that gave up mid-frame
        — so the line's absence IS the raster path, and the panel is one line
        tall when the stage is not on the GPU.
        """
        layer = self._controller.light_layer
        if layer is None:
            return []
        stats = layer.debug_stats()
        if stats["state"] != "ready":
            return []

        width, height = stats["surface"]
        lights = str(stats["lights"]).rjust(_COUNT_WIDTH)
        lines = [f"draw {width}×{height} · {lights} lights"]
        if stats["fog"]:
            # The smoke rides here rather than with the lights because it is
            # the medium the march reads, not something that glows — and
            # because `_pack` only separates parcels out at all when the march
            # is running, so off the fog line the count would always be 0.
            fog_width, fog_height = stats["fog_size"]
            settling = stats["converge"]
            smoke = str(stats["media"]).rjust(_COUNT_WIDTH)
            status = (f"settling {settling}" if settling else "settled")
            lines.append(f"fog  {fog_width}×{fog_height} · {smoke} smoke · "
                         + status.rjust(_SETTLE_WIDTH))
        return lines
