"""
Stress test that reproduces the SIGSEGV crash in the timeline.

The crash occurs when a DraggableMidiEvent's mouseReleaseEvent emits a
commit signal whose handler destroys the item (via refresh_from_manager /
redraw_events) while Qt is still unwinding the mouse event delivery.
Qt's scene then tries to access the dead item's vtable → SIGSEGV.

This test simulates REAL mouse events through QGraphicsView so the full
Qt event-delivery → signal → refresh → item-destruction path is exercised.

Run with:
    uv run python tests/test_crash_repro.py           # test the deferred-refresh fix
    uv run python tests/test_crash_repro.py --sync    # test the old (broken) sync path
"""

import sys
import os
import gc
import time
import random
from dataclasses import dataclass

# Ensure the project root is on sys.path when run from tests/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PyQt6.QtWidgets import QApplication, QMainWindow
from PyQt6.QtCore import QTimer, Qt, QPointF, QPoint
from PyQt6.QtGui import QMouseEvent, QWheelEvent
from PyQt6.QtTest import QTest

from midi.manager import MidiManager
from timeline.manager import Timeline
from timeline.tracks.midi_events import DraggableMidiEvent


@dataclass
class FakeNoteInfo:
    part: str
    color: str
    fixture: str = "ledbar"


# Minimal note map with a few ledbar and blinder notes
FAKE_NOTE_MAP = {
    "12": FakeNoteInfo(part="Scene Select 1", color="#ff0000", fixture="ledbar"),
    "13": FakeNoteInfo(part="Scene Select 2", color="#00ff00", fixture="ledbar"),
    "14": FakeNoteInfo(part="Scene Select 3", color="#0000ff", fixture="ledbar"),
    "15": FakeNoteInfo(part="Scene Select 4", color="#ffff00", fixture="ledbar"),
    "26": FakeNoteInfo(part="Blinder Flash", color="#ffffff", fixture="blinder"),
    "27": FakeNoteInfo(part="Blinder Pulse", color="#ff8800", fixture="blinder"),
    "38": FakeNoteInfo(part="BLACKOUT", color="#000000", fixture="ledbar"),
    "39": FakeNoteInfo(part="BLACKOUT_BLINDERS", color="#000000", fixture="blinder"),
}


class CrashReproWindow(QMainWindow):
    """Minimal window hosting a Timeline hammered with simulated mouse events."""

    SYNC_REFRESH = False  # Subclass overrides to True for the broken path

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Timeline Crash Repro")
        self.resize(1200, 400)

        self.midi_manager = MidiManager(midi_note_map=FAKE_NOTE_MAP)
        self.midi_manager.create_empty_track()

        self.timeline = Timeline(
            midi_manager=self.midi_manager,
            parent=self,
        )
        self.setCentralWidget(self.timeline)

        # Connect data_changed → refresh (mimics main.py)
        self.midi_manager.data_changed.connect(self._on_data_changed)

        self._seed_events()

        self.ops_done = 0
        self.max_ops = 500
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._do_random_operation)
        self.timer.start(1)

    def _on_data_changed(self):
        if self.SYNC_REFRESH:
            self.timeline.refresh_from_manager()
        else:
            self.timeline._schedule_refresh()

    def _seed_events(self):
        notes = [12, 13, 14, 15, 26, 27]
        for i in range(20):
            note = random.choice(notes)
            time = i * 2.0 + random.uniform(0.1, 0.9)
            velocity = random.randint(40, 127)
            self.midi_manager.add_event(note, time, velocity)
        self.timeline.refresh_from_manager()

    # ------------------------------------------------------------------
    # Helpers to find scene items and map positions
    # ------------------------------------------------------------------

    def _get_clip_items(self):
        """Return all DraggableMidiEvent items currently in the scene."""
        items = []
        for item in self.timeline.content_scene.items():
            if isinstance(item, DraggableMidiEvent) and not item.is_blackout_clip:
                items.append(item)
        return items

    def _scene_to_view(self, scene_pos: QPointF) -> QPoint:
        """Map a scene position to a viewport-local pixel position."""
        return self.timeline.content_view.mapFromScene(scene_pos)

    # ------------------------------------------------------------------
    # Simulated mouse gestures that go through the full Qt event pipeline
    # ------------------------------------------------------------------

    def _simulate_move_drag(self, clip: DraggableMidiEvent):
        """Press in clip center, drag +-30px, release."""
        view = self.timeline.content_view
        viewport = view.viewport()

        center = clip.mapToScene(clip.boundingRect().center())
        start_vp = self._scene_to_view(center)

        # Skip if not visible
        if not viewport.rect().contains(start_vp):
            return

        dx = random.randint(-30, 30)
        end_vp = QPoint(max(0, start_vp.x() + dx), start_vp.y())

        QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start_vp)
        # Intermediate move to pass the 1px "did we move?" threshold
        mid = QPoint(start_vp.x() + (dx // 2 or 1), start_vp.y())
        QTest.mouseMove(viewport, mid)
        QTest.mouseMove(viewport, end_vp)
        QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=end_vp)

        # Force a synchronous repaint right after release — this is the
        # dangerous window where stale scene items may be accessed.
        viewport.repaint()
        gc.collect()
        viewport.repaint()

    def _simulate_resize_drag(self, clip: DraggableMidiEvent, side: str):
        """Press on left/right edge, drag a few pixels, release."""
        view = self.timeline.content_view
        viewport = view.viewport()

        rect = clip.boundingRect()
        if side == "left":
            edge_local = QPointF(2, rect.height() / 2)
        else:
            edge_local = QPointF(rect.width() - 2, rect.height() / 2)

        edge_scene = clip.mapToScene(edge_local)
        start_vp = self._scene_to_view(edge_scene)

        if not viewport.rect().contains(start_vp):
            return

        dx = random.randint(-15, 15)
        end_vp = QPoint(max(0, start_vp.x() + dx), start_vp.y())

        QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start_vp)
        QTest.mouseMove(viewport, end_vp)
        QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=end_vp)

        viewport.repaint()
        gc.collect()
        viewport.repaint()

    def _simulate_api_mutate_during_paint(self):
        """Mutate data + synchronous refresh + repaint (API path)."""
        events = self.midi_manager.get_events_for_ui()
        non_bo = [e for e in events if not self._is_blackout(e)]
        if not non_bo:
            return

        e = random.choice(non_bo)
        op = random.choice(["move", "delete", "edit", "resize_left", "resize_right"])

        if op == "move":
            self.midi_manager.move_event(e["id"],
                                         max(0, e["time"] + random.uniform(-2, 2)))
        elif op == "delete":
            self.midi_manager.delete_event(e["id"])
        elif op == "edit":
            self.midi_manager.edit_event(e["id"],
                                          velocity=random.randint(40, 127))
        elif op == "resize_left":
            self.midi_manager.set_event_left_edge(
                e["id"], max(0, e["time"] + random.uniform(-0.5, 0.5)))
        elif op == "resize_right":
            self.midi_manager.set_event_right_edge(
                e["id"], e["time"] + random.uniform(0.5, 3.0))

        if self.SYNC_REFRESH:
            self.timeline.refresh_from_manager()
        else:
            self.timeline._schedule_refresh()

        self.timeline.content_view.viewport().repaint()
        gc.collect()
        self.timeline.content_view.viewport().repaint()

    def _simulate_insert_and_repaint(self):
        notes = [12, 13, 14, 15, 26, 27]
        self.midi_manager.add_event(
            random.choice(notes),
            random.uniform(0, 50.0),
            random.randint(40, 127),
        )
        if self.SYNC_REFRESH:
            self.timeline.refresh_from_manager()
        else:
            self.timeline._schedule_refresh()

        self.timeline.content_view.viewport().repaint()
        gc.collect()
        self.timeline.content_view.viewport().repaint()

    def _simulate_zoom(self):
        """Simulate Ctrl+wheel zoom — the exact user-reported crash trigger.

        Zooming calls view.scale() which invalidates the entire viewport,
        then update_midi_text_truncation() iterates scene items. If stale
        items haven't been fully removed from Qt's BSP index, the repaint
        triggered by scale() hits a freed object.
        """
        view = self.timeline.content_view
        viewport = view.viewport()
        center = QPoint(viewport.width() // 2, viewport.height() // 2)

        # Build a Ctrl+WheelEvent (zoom in or out randomly)
        delta = random.choice([120, -120])
        event = QWheelEvent(
            QPointF(center),                           # pos
            viewport.mapToGlobal(center).toPointF(),   # globalPos
            QPoint(0, delta),                          # pixelDelta
            QPoint(0, delta),                          # angleDelta
            Qt.MouseButton.NoButton,                   # buttons
            Qt.KeyboardModifier.ControlModifier,       # modifiers
            Qt.ScrollPhase.NoScrollPhase,              # phase
            False,                                     # inverted
        )
        QApplication.sendEvent(viewport, event)

        # Force repaint + GC to maximize crash window
        viewport.repaint()
        gc.collect()
        viewport.repaint()

    def _is_blackout(self, event):
        info = event.get("info")
        part = getattr(info, "part", "") if info else ""
        return part.upper().startswith("BLACKOUT")

    # ------------------------------------------------------------------
    # Main stress loop
    # ------------------------------------------------------------------

    def _do_random_operation(self):
        self.ops_done += 1
        if self.ops_done > self.max_ops:
            self.timer.stop()
            print(f"\n=== PASSED: {self.max_ops} operations without crash ===")
            QApplication.instance().quit()
            return

        # Weighted toward mouse-event paths (the real crash vector)
        clips = self._get_clip_items()
        op = random.choices(
            ["mouse_move", "mouse_resize_left", "mouse_resize_right",
             "api_mutate", "insert", "zoom"],
            weights=[25, 12, 12, 20, 12, 19],
            k=1,
        )[0]

        try:
            if op == "mouse_move" and clips:
                self._simulate_move_drag(random.choice(clips))
            elif op == "mouse_resize_left" and clips:
                self._simulate_resize_drag(random.choice(clips), "left")
            elif op == "mouse_resize_right" and clips:
                self._simulate_resize_drag(random.choice(clips), "right")
            elif op == "api_mutate":
                self._simulate_api_mutate_during_paint()
            elif op == "insert":
                self._simulate_insert_and_repaint()
            elif op == "zoom":
                self._simulate_zoom()
            else:
                self._simulate_insert_and_repaint()
        except Exception as e:
            pass  # don't stop on non-crash errors

        if self.ops_done % 50 == 0:
            n_events = len(self.midi_manager.get_events_for_ui())
            print(f"  [{self.ops_done}/{self.max_ops}] op={op:<20s} "
                  f"events={n_events}, clips={len(clips)}")


class SyncRefreshCrashWindow(CrashReproWindow):
    """Uses synchronous refresh_from_manager() — the original crash path."""
    SYNC_REFRESH = True


def test_no_crash_under_random_drag_stress(qapp):
    """Deferred-refresh path survives 500 random drag/edit ops (no SIGSEGV).

    Drives the real QTimer-based stress loop via processEvents (rather than
    app.exec) so it cooperates with pytest's process-shared QApplication. Only
    the deferred-refresh window is exercised — the --sync window reproduces the
    original crash and is for manual runs via main().
    """
    window = CrashReproWindow()
    window.show()
    deadline = time.monotonic() + 120
    while window.ops_done <= window.max_ops and time.monotonic() < deadline:
        qapp.processEvents()
    try:
        assert window.ops_done > window.max_ops, "stress loop did not finish in time"
    finally:
        window.close()


def main():
    app = QApplication(sys.argv)

    sync_mode = "--sync" in sys.argv
    if sync_mode:
        print("=== SYNC MODE: synchronous refresh (crash expected) ===\n")
        window = SyncRefreshCrashWindow()
    else:
        print("=== DEFERRED MODE: _schedule_refresh (should pass) ===\n")
        window = CrashReproWindow()

    window.show()
    print(f"Will perform {window.max_ops} random operations.")
    print("If the app crashes with SIGSEGV, the bug is reproduced.\n")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
