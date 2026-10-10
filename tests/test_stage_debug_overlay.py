"""
Characterization test: the stage's debug readout.

Covers what the panel promises — it appears only at DEBUG log level and takes
its frame probe with it, it sits in the stage image's bottom-right corner
without disturbing the top-right row, and it writes a `draw` line only when the
GPU light layer is genuinely drawing, so that line's absence is the raster
path.

The GPU half is exercised through a stand-in layer: CI's offscreen platform
cannot make a GL context, so the real one never initialises there — but
`debug_stats()` is a plain dict and what the panel does with it is the part
worth pinning down.

Offscreen Qt + a shared QApplication come from conftest.py (the `qapp` fixture).

Run with:
    uv run python tests/test_stage_debug_overlay.py
"""

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PyQt6.QtWidgets import QPushButton
from core.config import Config, FixtureInstance


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _stage():
    from stage.controller import StageController
    cfg = Config()
    cfg.fixtures = [
        FixtureInstance(type="blinder", dmx_start=33, x=575, y=500),
    ]
    return StageController(cfg), cfg


class _FakeLayer:
    """A light layer that never touches GL; only `debug_stats` is read."""

    def __init__(self, **stats):
        self.stats = {
            "state": "ready", "surface": (1920, 1080), "lights": 12,
            "media": 3, "fog": False, "fog_size": (0, 0), "converge": 0,
        }
        self.stats.update(stats)

    def debug_stats(self):
        return self.stats


def _at_level(level, overlay):
    logging.getLogger().setLevel(level)
    overlay.refresh()


def test_appears_only_at_debug(qapp):
    print("test_appears_only_at_debug")
    sc, _ = _stage()
    view, overlay = sc.get_stage_view(), sc.debug_overlay
    try:
        _assert(overlay.isHidden(), "the panel starts hidden")
        _assert(view._frame_probe is None, "nothing times the paints yet")

        _at_level(logging.DEBUG, overlay)
        _assert(not overlay.isHidden(), "DEBUG shows the panel")
        _assert(view._frame_probe is not None, "and installs the frame probe")

        _at_level(logging.INFO, overlay)
        _assert(overlay.isHidden(), "back to INFO hides it again")
        _assert(view._frame_probe is None, "and uninstalls the probe")
    finally:
        logging.getLogger().setLevel(logging.WARNING)


def test_sits_in_the_stage_bottom_right(qapp):
    print("test_sits_in_the_stage_bottom_right")
    sc, _ = _stage()
    view, overlay = sc.get_stage_view(), sc.debug_overlay
    top_right = QPushButton("Edit stage")
    top_right.adjustSize()
    view.add_overlay(top_right, anchor="top-right", reference="background")
    top_right.show()
    view.resize(1200, 700)
    view.show()
    try:
        _at_level(logging.DEBUG, overlay)
        stage_rect = view._background_rect()
        panel = overlay.geometry()
        _assert(panel.bottom() <= stage_rect.bottom(),
                "the panel stays inside the stage image's bottom edge")
        _assert(panel.right() <= stage_rect.right(),
                "and inside its right edge")
        _assert(panel.top() > stage_rect.center().y(),
                "it hangs off the bottom rather than the top")
        _assert(panel.top() > top_right.geometry().bottom(),
                "the top-right row keeps its corner")
    finally:
        logging.getLogger().setLevel(logging.WARNING)
        view.hide()


def test_the_raster_path_says_nothing(qapp):
    print("test_the_raster_path_says_nothing")
    sc, _ = _stage()
    overlay = sc.debug_overlay
    _assert(overlay._gpu_lines() == [],
            "with no light layer there is nothing to report about the GPU")
    _assert(overlay._report().count("\n") == 0,
            "so the panel is the frame-rate line alone")

    sc.light_layer = _FakeLayer(state="failed")
    _assert(overlay._gpu_lines() == [],
            "a layer that surrendered mid-frame has drawn nothing")

    sc.light_layer = _FakeLayer(state="starting")
    _assert(overlay._gpu_lines() == [],
            "nor has one that has not initialised yet")


def test_reports_what_the_layer_drew(qapp):
    print("test_reports_what_the_layer_drew")
    sc, _ = _stage()
    overlay = sc.debug_overlay

    sc.light_layer = _FakeLayer()
    report = overlay._report()
    _assert("1920×1080" in report, "the surface it draws into is reported")
    _assert("12 lights" in report, "so is the number of light primitives")
    _assert("smoke" not in report,
            "with the march off there are no parcels to count")
    _assert("fog" not in report, "and no fog line at all")

    sc.light_layer = _FakeLayer(fog=True, fog_size=(960, 540), converge=4)
    report = overlay._report()
    _assert("960×540" in report, "the march's own resolution is reported")
    _assert("3 smoke" in report.split("\n")[-1],
            "the smoke rides the fog line, with the medium it feeds")
    _assert("settling 4" in report, "and the march says it is converging")

    sc.light_layer = _FakeLayer(fog=True, fog_size=(960, 540), converge=0)
    _assert("settled" in overlay._report(), "a converged march says so")


def test_the_block_never_moves(qapp):
    print("test_the_block_never_moves")
    sc, _ = _stage()
    overlay = sc.debug_overlay

    def widths(**stats):
        sc.light_layer = _FakeLayer(fog=True, fog_size=(960, 540), **stats)
        return [len(line) for line in overlay._report().split("\n")]

    lean = widths(lights=7, media=0, converge=5)
    grown = widths(lights=1999, media=96, converge=5)
    _assert(lean == grown,
            "a count growing from one digit to four changes no line's width")
    _assert(widths(lights=7, media=0, converge=0) == lean,
            "nor does the march finishing converging")


def test_frame_rate_follows_the_paints(qapp):
    print("test_frame_rate_follows_the_paints")
    from stage.debug_overlay import WINDOW_S
    sc, _ = _stage()
    overlay = sc.debug_overlay

    import time
    # Long before the window on the overlay's own clock. Not a fixed
    # timestamp: perf_counter counts from boot, and on a CI machine that
    # booted a few minutes ago a fixed 1000.0 lands inside the window.
    long_ago = time.perf_counter() - 10 * WINDOW_S
    for i in range(60):
        start = long_ago + i / 60.0
        overlay._on_frame(start, start + 0.004)
    fps, last, worst = overlay._rates()
    _assert(fps == 0.0, "frames older than the window count for nothing")

    now = time.perf_counter()
    for i in range(20):
        start = now - 1.0 + i * 0.05
        overlay._on_frame(start, start + (0.030 if i == 7 else 0.004))
    fps, last, worst = overlay._rates()
    _assert(fps == 20 / WINDOW_S, "the rate is the frames inside the window")
    _assert(abs(last - 0.004) < 1e-6, "the last paint is the last one measured")
    _assert(abs(worst - 0.030) < 1e-6,
            "the worst paint in the window survives, which is the stall")


def test_a_still_stage_repaints_nothing(qapp):
    print("test_a_still_stage_repaints_nothing")
    sc, _ = _stage()
    overlay = sc.debug_overlay
    try:
        _at_level(logging.DEBUG, overlay)
        first = overlay.text()
        _assert(first != "", "the panel has a report once it is showing")
        overlay.refresh()
        _assert(overlay.text() == first,
                "an unchanged stage produces the very same text")
    finally:
        logging.getLogger().setLevel(logging.WARNING)


def main():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    test_appears_only_at_debug(app)
    test_sits_in_the_stage_bottom_right(app)
    test_the_raster_path_says_nothing(app)
    test_reports_what_the_layer_drew(app)
    test_the_block_never_moves(app)
    test_frame_rate_follows_the_paints(app)
    test_a_still_stage_repaints_nothing(app)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
