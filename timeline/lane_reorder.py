"""
Carrying a lane header to a new row.

The gesture behind the grip at the head of every fixture lane: press, carry,
restack the lanes it passes, and on release announce the new order so the show
can persist it. Split out of the timeline because it is the one gesture with
state of its own — a drag dict, a watchdog and an application-wide event
filter — that nothing else in the widget touches.

An inner collaborator of the timeline, not a stranger to it: it reads the
strip's layout helpers and reorders its track list in place.
"""

import time

from PyQt6.QtCore import QEvent, QObject, QPoint, Qt, QTimer
from PyQt6.QtWidgets import QApplication


class LaneReorder(QObject):
    """Drives one header-grip drag at a time for `timeline`."""

    def __init__(self, timeline):
        # Parented to the timeline: the controller dies with the widget whose
        # lanes it carries, and so does its watchdog.
        super().__init__(timeline)
        self._timeline = timeline
        # The in-flight drag (a header grip is held), or None, plus the
        # watchdog that ends one whose release never arrived.
        self.drag = None
        self._watchdog = None

    def begin(self, track):
        """A header grip was pressed: lift that lane's header off the strip.

        The gesture is anchored on the first MOVE rather than here: a press and
        a move at the same cursor position arrive through the graphics proxy a
        pixel apart, and that pixel would show up as the lane starting the drag
        already displaced."""
        if track not in self._timeline.midi_events_tracks:
            return
        row = self._timeline.midi_events_tracks.index(track)
        self.drag = {
            "track": track,
            "row_at_press": row,
            "top_at_press": float(self._timeline._midi_track_y(row)),
            "grab_offset": None,
            # The strip is pinned for the duration: nothing about this gesture
            # scrolls it, so anything that does is moving the ground under a
            # lane whose position is measured against the scene.
            "scroll_at_press": self._timeline.header_view.verticalScrollBar().value(),
            "moves": 0,
            "crossings": 0,
            "anomalies": [],
            "busy": False,
            "last_move_at": time.monotonic(),
        }
        track.set_header_lifted(True)
        # A release that lands anywhere else in the app still ends the drag: the
        # scene hands the grab back the moment a move arrives with no button
        # held, so the grip cannot be relied on to see its own release.
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        if self._watchdog is None:
            self._watchdog = QTimer(self)
            self._watchdog.setInterval(250)
            self._watchdog.timeout.connect(self._check_alive)
        self._watchdog.start()

    def eventFilter(self, obj, event):
        """Drive and end a reorder drag from the header view's own events.

        The carry rides the VIEWPORT's mouse moves, whose `position()` is
        surface-local — the only kind of coordinate a Wayland client can trust.
        The viewport keeps the widget-level grab for the whole gesture, so its
        events follow the cursor even outside the strip.

        Two things end the drag, because the grip may see neither: the release
        itself, wherever in the app it lands, and any mouse MOVEMENT with the
        button no longer held. The scene hands the grab back as soon as a move
        arrives unpressed — it becomes a hover, the grip is never told, and the
        lane would go on following the cursor with nothing held down. Hover
        events are their own type, so a live drag never trips that."""
        if self.drag is not None:
            kind = event.type()
            if kind == QEvent.Type.MouseButtonRelease:
                self.end()
            elif kind == QEvent.Type.MouseMove:
                if not (event.buttons() & Qt.MouseButton.LeftButton):
                    self._note_anomaly(
                        self.drag, "unpressed-move",
                        "the mouse moved with no button held — ending a drag "
                        "whose release never reached the grip")
                    self.end()
                elif obj is self._timeline.header_view.viewport():
                    self.update(event.position().y())
        return super().eventFilter(obj, event)

    def _check_alive(self) -> None:
        """Drop a carried lane whose release the application never saw at all —
        the button came up over another window, or the grab was taken away.

        Both conditions are needed: the button being up is not enough on its own
        (a synthetic drag never sets the global button state), and neither is
        the silence (a drag can rest on a row without moving)."""
        drag = self.drag
        if drag is None:
            return
        held = QApplication.mouseButtons() & Qt.MouseButton.LeftButton
        if held or time.monotonic() - drag["last_move_at"] < 0.4:
            return
        self._note_anomaly(
            drag, "lost-release",
            "no button held and no movement — ending a drag whose release "
            "never arrived")
        self.end()

    def update(self, viewport_y: float):
        """Carry the lifted header with the cursor, restacking the lanes it
        passes. `viewport_y` is measured inside the header view's viewport.

        The restack is the whole visual: the tracks list is the layout, so
        moving the dragged lane inside it slides every other header, its clips
        and its lane tint into the new stacking — leaving a gap exactly where
        the carried lane will land. Only a crossing restacks; the frames
        between just move one proxy.

        The position is resolved against the scene on every move, so the lane
        stays under the cursor even if the strip scrolls mid-drag."""
        drag = self.drag
        if drag is None:
            return
        if drag["busy"]:
            # A restack moves proxies and items under the cursor; if Qt turns
            # that into another mouse event we would recurse into our own
            # layout, each level restacking again.
            self._note_anomaly(drag, "reentry", "re-entered mid-restack")
            return
        drag["busy"] = True
        try:
            self._carry(drag, viewport_y)
        finally:
            drag["busy"] = False

    def _carry(self, drag, viewport_y: float) -> None:
        """One frame of the carry: hold the strip still, place the lane, and
        restack if it has changed rows."""
        drag["moves"] += 1
        drag["last_move_at"] = time.monotonic()
        bar = self._timeline.header_view.verticalScrollBar()
        if bar.value() != drag["scroll_at_press"]:
            self._note_anomaly(
                drag, "scrolled",
                f"the strip scrolled under the drag "
                f"({drag['scroll_at_press']} -> {bar.value()})")
            bar.setValue(drag["scroll_at_press"])

        scene_y = self._scene_y(viewport_y)
        if drag["grab_offset"] is None:
            drag["grab_offset"] = scene_y - drag["top_at_press"]
        track = drag["track"]
        height = track.get_track_height()
        top = scene_y - drag["grab_offset"]
        # The carried lane stays inside the strip (the total height is the same
        # whatever the order, so the floor doesn't move as the lanes restack).
        top = max(float(self._timeline._midi_tracks_top()),
                  min(float(self._timeline._midi_tracks_bottom() - height), top))

        rows = self._timeline.midi_events_tracks
        target = self._target_row(track, top)
        if target != rows.index(track):
            drag["crossings"] += 1
            rows.insert(target, rows.pop(rows.index(track)))
            self._restack()
        # After the restack, which puts every header back on the row grid.
        track.float_header_to(top)

    def _note_anomaly(self, drag, kind: str, detail: str) -> None:
        """Record something a reorder drag should never see, once per kind per
        drag. These are the symptoms of the strip 'going crazy', and the log is
        the only witness when it happens on a machine we can't drive.

        Only conditions that cannot happen in normal use live here — a lane
        moving a long way in one event is NOT one of them, since a fast flick
        does exactly that. The end-of-drag line's crossing count is the honest
        signal for a carry that is not tracking the cursor."""
        if kind in drag["anomalies"]:
            return
        drag["anomalies"].append(kind)
        self._timeline.logger.warning(f"Track reorder anomaly [{kind}]: {detail}")

    def _restack(self):
        """Restack the lanes mid-drag: the cheap half of `_reflow_layout`.

        Nothing about a clip changes when lanes swap rows — only the y its lane
        sits at — and the scene height is the same whatever the order, so the
        rects, the grid, the ruler, the waveform and the scroll ranges are all
        already right. What is left is moving each lane's header and content,
        and repainting the lane tints that say which row belongs to whom.

        The full pass runs once, on release. Doing it per crossing instead cost
        ~65ms a time on a 240-clip song, which is several mouse events' worth:
        the drag then lurched from one compressed position to the next."""
        for row, track in enumerate(self._timeline.midi_events_tracks):
            track.move_content_to(self._timeline._midi_track_y(row))
        self._timeline.draw_lane_bands()

    def _scene_y(self, viewport_y: float) -> float:
        """Where a y inside the header viewport lands in the header scene.

        Viewport coordinates in, deliberately: a screen position would have to
        be mapped back through the window's origin, and under Wayland that
        origin is a guess — the round trip is not the identity, and when it
        shifted mid-drag the lane jumped the height of the whole strip.

        Resolved through the view on every move rather than as a delta from the
        press, because the strip scrolls: its scrollbar is hidden, but it is
        driven by the content view's."""
        return self._timeline.header_view.mapToScene(
            QPoint(0, int(round(viewport_y)))).y()

    def _target_row(self, track, top: float) -> int:
        """The row `track` belongs in with its header carried to `top`: the
        insertion slot whose own top the carried one is nearest.

        Slots are the tops the lanes would have with the dragged one taken out
        (they close up behind it), so slot k lands exactly where the lane
        rested before it was picked up — which is what stops a lane from
        swapping with its neighbour the moment it is grabbed. Nearest-slot then
        puts the swap at half the height of the lane being passed, the same
        coming back up as going down; measuring the carried lane's CENTRE
        against those closed-up neighbours instead puts a lane at rest exactly
        on the next slot's tie point, and one pixel of travel restacks it.

        Heights are walked rather than multiplied, since a collapsed lane is
        shorter than the rest — so passing one only costs half of ITS height.
        The answer can't flap: the carried top follows the cursor, and the slots
        don't depend on the row the dragged lane currently sits in."""
        tops = [float(self._timeline._midi_tracks_top())]
        for other in self._timeline.midi_events_tracks:
            if other is not track:
                tops.append(tops[-1] + other.get_track_height())
        return min(range(len(tops)), key=lambda row: abs(top - tops[row]))

    def end(self):
        """The grip was released: settle the header back onto the row grid and,
        if the lane actually moved, announce the new order for persistence."""
        drag = self.drag
        if drag is None:
            return
        self.drag = None
        if self._watchdog is not None:
            self._watchdog.stop()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        track = drag["track"]
        track.set_header_lifted(False)
        self._timeline._reflow_layout()
        self._timeline.logger.info(
            f"Track reorder ended: {drag['moves']} moves, "
            f"{drag['crossings']} crossings, row {drag['row_at_press']} -> "
            f"{self._timeline.midi_events_tracks.index(track)}"
            + (f", ANOMALIES {drag['anomalies']}" if drag["anomalies"] else ""))
        if self._timeline.midi_events_tracks.index(track) == drag["row_at_press"]:
            return
        order = [t.fixture_index for t in self._timeline.midi_events_tracks]
        self._timeline.logger.info(f"Timeline track order changed (channels): {order}")
        self._timeline.track_order_changed.emit(order)

