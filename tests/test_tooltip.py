"""
Characterization test: the timeline's persistent tooltip.

It is a CHILD widget, not a `Qt.WindowType.ToolTip` one. That is a performance
property, not a cosmetic one: a tooltip window is a native top-level window and
mapping one was measured blocking the Qt thread for 111 ms on the first hover,
with the section drag re-showing it on every mouse move. None of that is
reproducible offscreen (there is no compositor to round-trip to), so what is
asserted here is the shape that caused it.

Run with:
    uv run python tests/test_tooltip.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtWidgets import QApplication, QWidget

from timeline.widgets.tooltip import CustomTooltip


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _parent():
    parent = QWidget()
    parent.resize(800, 600)
    parent.show()
    return parent


def test_tooltip_has_no_window_of_its_own():
    print("test_tooltip_has_no_window_of_its_own")
    parent = _parent()
    tip = CustomTooltip(parent)
    _assert(not tip.isWindow(),
            "the tooltip is a child widget — a top-level one costs a "
            "compositor round trip every time it is shown")
    tip.show_at(parent.mapToGlobal(QPoint(100, 100)), "hello")
    _assert(tip.window() is parent.window(),
            "…and showing it does not give it one")


def test_tooltip_does_not_take_mouse_events():
    print("test_tooltip_does_not_take_mouse_events")
    parent = _parent()
    tip = CustomTooltip(parent)
    _assert(tip.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents),
            "it sits under the cursor, so taking mouse events would send the "
            "clip beneath it a hover-leave and flicker it away")


def test_tooltip_is_clamped_inside_its_parent():
    print("test_tooltip_is_clamped_inside_its_parent")
    parent = _parent()
    tip = CustomTooltip(parent)

    tip.show_at(parent.mapToGlobal(QPoint(5000, 5000)), "past the edge")
    _assert(tip.geometry().right() <= parent.width()
            and tip.geometry().bottom() <= parent.height(),
            f"a tooltip asked for past the corner is pulled back inside "
            f"({tip.geometry()}) — a child cannot overflow its parent the way "
            "a tooltip window could")

    tip.show_at(parent.mapToGlobal(QPoint(-500, -500)), "before the edge")
    _assert(tip.x() >= 0 and tip.y() >= 0,
            "and the same at the near corner")


def test_tooltip_flips_above_when_it_would_not_fit_below():
    print("test_tooltip_flips_above_when_it_would_not_fit_below")
    parent = _parent()
    tip = CustomTooltip(parent)
    tip.setText("near the bottom")
    tip.adjustSize()
    # The timeline sits at the BOTTOM of the window, so this is the ordinary
    # case for a clip tooltip, not an edge case. Less than the tooltip's own
    # height from the bottom, so it cannot fit below whatever the font makes
    # that height.
    anchor_y = parent.height() - tip.height() // 2
    tip.show_at(parent.mapToGlobal(QPoint(300, anchor_y)), "near the bottom")
    _assert(tip.geometry().bottom() < anchor_y,
            f"it goes ABOVE the cursor ({tip.geometry()}) rather than being "
            "clamped down onto the clip it is describing")
    _assert(tip.y() >= 0, "…and stays inside the parent")

    # With room below, it stays below.
    tip.show_at(parent.mapToGlobal(QPoint(300, 100)), "plenty of room")
    _assert(tip.y() >= 100,
            "with room below the cursor it stays below, as asked")


def test_tooltip_moves_without_re_showing():
    print("test_tooltip_moves_without_re_showing")
    parent = _parent()
    tip = CustomTooltip(parent)
    shows = []
    original = tip.show
    tip.show = lambda: (shows.append(1), original())

    tip.show_at(parent.mapToGlobal(QPoint(100, 100)), "one")
    first = tip.pos()
    # A section drag calls this on every mouse move, with changing text.
    for step in range(10):
        tip.show_at(parent.mapToGlobal(QPoint(120 + step, 140)), f"step {step}")

    _assert(len(shows) == 1,
            f"shown once, then only moved ({len(shows)} shows for 11 calls) — "
            "the repeated work is what a drag used to pay per frame")
    _assert(tip.pos() != first, "…and it did follow the cursor")
    _assert(tip.isVisible(), "…and it is still up")


def test_hidden_tooltip_shows_again():
    print("test_hidden_tooltip_shows_again")
    parent = _parent()
    tip = CustomTooltip(parent)
    tip.show_at(parent.mapToGlobal(QPoint(100, 100)), "one")
    tip.hide_tooltip()
    _assert(not tip.isVisible(), "hide_tooltip hides it")
    tip.show_at(parent.mapToGlobal(QPoint(100, 100)), "one")
    _assert(tip.isVisible(),
            "and the SAME text brings it back — the early-out on unchanged "
            "text must not skip the show")


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
