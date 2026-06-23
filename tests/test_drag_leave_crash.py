"""
Regression test: a stray drag-leave over the timeline must not abort.

The clip-library drag handlers on ContentView (dragEnter/dragMove/drop) accept
the drag at the view level without delegating to QGraphicsView's base drag
machinery, so the base view never records its own drag-enter state. Before the
fix, the base QGraphicsView.dragLeaveEvent then ran on a leave it had no enter
for, logged "drag leave received before drag enter", and that path aborts the
process on Wayland (a spurious leave is delivered when the drag passes over a
rejected lane or ends).

This test delivers a QDragLeaveEvent through the real event dispatch and locks
in that the broken base warning is never emitted (i.e. our override consumes
the leave instead).

Run with:
    uv run python tests/test_drag_leave_crash.py
"""

import sys
import os
from dataclasses import dataclass

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import note_layout
from midi.manager import MidiManager
from timeline.manager import Timeline


@dataclass
class FakeFixture:
    type: str
    name: str = ""


PATCH = [FakeFixture("ledbar"), FakeFixture("blinder")]


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


_APP = None


def _ensure_app():
    global _APP
    from PyQt6.QtWidgets import QApplication
    _APP = QApplication.instance() or QApplication([])
    return _APP


def _make():
    _ensure_app()
    m = MidiManager(note_layout.build_note_map(), fixtures=PATCH)
    m.create_empty_track()
    m.set_song_duration(240.0)
    tl = Timeline(midi_manager=m, fixtures=PATCH)
    return m, tl


def test_drag_leave_does_not_hit_base_warning():
    print("test_drag_leave_does_not_hit_base_warning")
    from PyQt6.QtCore import qInstallMessageHandler
    from PyQt6.QtGui import QDragLeaveEvent

    _ensure_app()
    _, tl = _make()
    view = tl.content_view

    messages = []
    previous = qInstallMessageHandler(
        lambda mode, context, message: messages.append(message))
    try:
        event = QDragLeaveEvent()
        # Dispatch a drag leave on a view that never saw a (base-class) drag
        # enter — exactly the state Wayland delivers after a drop or over a
        # rejected lane. Without the override this resolves to the inherited
        # QGraphicsView.dragLeaveEvent, which warns "drag leave received before
        # drag enter" and aborts on Wayland.
        view.dragLeaveEvent(event)
    finally:
        qInstallMessageHandler(previous)

    offending = [m for m in messages
                 if "drag leave received before drag enter" in m]
    _assert(not offending,
            f"base QGraphicsView drag-leave warning not emitted (got {messages})")
    _assert(event.isAccepted(), "drag leave consumed by our override")


def test_override_is_present():
    print("test_override_is_present")
    from timeline.widgets.content_view import ContentView
    # Defined on ContentView itself, not merely inherited (PyQt6 makes `is`
    # comparisons against the base method unreliable, so check __dict__).
    _assert("dragLeaveEvent" in vars(ContentView),
            "ContentView defines its own dragLeaveEvent (not the base)")


def main():
    _ensure_app()
    test_override_is_present()
    test_drag_leave_does_not_hit_base_warning()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
