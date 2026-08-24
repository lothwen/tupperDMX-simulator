"""
Unit test: pure clip-drag geometry (timeline/tracks/clip_geometry.py).

Covers the Qt-free math extracted from the timeline: zone detection, neighbour
/ beat snapping, interval clamping, MOVE/RESIZE resolution, and bounds
computation (no-overlap, BLACKOUT absorption, preserve-width-on-move, the
point-sized last clip).

Run with:
    uv run python tests/test_clip_geometry.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from timeline.tracks.clip_geometry import (
    Occupant, ClipBounds,
    MODE_MOVE, MODE_RESIZE_LEFT, MODE_RESIZE_RIGHT,
    zone_at, nearest_beat_x, uniform_grid, snap_to_targets, clamp_to_intervals,
    resolve_move_x, resolve_resize_x, compute_bounds,
)

MIN_DUR = 0.01


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _close(a, b, tol=1e-6):
    return abs(a - b) <= tol


def test_zone_at():
    print("test_zone_at")
    # Wide clip (width 100, 8px edge zones).
    _assert(zone_at(4, 100, 8) == MODE_RESIZE_LEFT, "left edge -> RESIZE_LEFT")
    _assert(zone_at(8, 100, 8) == MODE_RESIZE_LEFT, "exactly at left zone -> RESIZE_LEFT")
    _assert(zone_at(50, 100, 8) == MODE_MOVE, "middle -> MOVE")
    _assert(zone_at(92, 100, 8) == MODE_RESIZE_RIGHT, "exactly at right zone -> RESIZE_RIGHT")
    _assert(zone_at(96, 100, 8) == MODE_RESIZE_RIGHT, "right edge -> RESIZE_RIGHT")
    # Narrow clip (width 10 <= 2*8): no MOVE zone, split half/half.
    _assert(zone_at(4, 10, 8) == MODE_RESIZE_LEFT, "narrow clip left half -> RESIZE_LEFT")
    _assert(zone_at(6, 10, 8) == MODE_RESIZE_RIGHT, "narrow clip right half -> RESIZE_RIGHT")
    # Multi-selected: always MOVE.
    _assert(zone_at(4, 100, 8, multi_selected=True) == MODE_MOVE,
            "multi-selected clip is all MOVE")


def test_nearest_beat_x():
    print("test_nearest_beat_x")
    _assert(_close(nearest_beat_x(23, 10), 20), "23 snaps to beat 20")
    _assert(_close(nearest_beat_x(27, 10), 30), "27 snaps to beat 30")
    _assert(_close(nearest_beat_x(10, 10), 10), "10 stays on beat 10")


def test_snap_to_targets():
    print("test_snap_to_targets")
    _assert(_close(snap_to_targets(100, [105, 130], 6), 105),
            "snaps to the in-radius target")
    _assert(_close(snap_to_targets(100, [130], 6), 100),
            "no snap when the only target is out of radius")
    _assert(_close(snap_to_targets(100, [104, 97], 6), 97),
            "picks the nearest of several in-radius targets")
    _assert(_close(snap_to_targets(100, [96, 104], 6), 96),
            "ties resolve to the first encountered")
    _assert(_close(snap_to_targets(100, [], 6), 100), "no candidates -> unchanged")


def test_clamp_to_intervals():
    print("test_clamp_to_intervals")
    _assert(clamp_to_intervals(50, []) is None, "no intervals -> None")
    x, iv = clamp_to_intervals(20, [(0, 40)])
    _assert(_close(x, 20) and iv == (0, 40), "inside the only interval -> unchanged")
    x, iv = clamp_to_intervals(55, [(0, 40), (60, 100)])
    _assert(_close(x, 60) and iv == (60, 100), "clamps to the closest interval")


def test_resolve_move_x():
    print("test_resolve_move_x")
    _assert(_close(resolve_move_x(20, [(0, 40)], [], 6), 20),
            "no snap targets -> clamped position")
    _assert(_close(resolve_move_x(20, [(0, 40)], [23], 6), 23),
            "snaps to an in-radius neighbour edge")
    _assert(_close(resolve_move_x(38, [(0, 40)], [44], 8), 40),
            "a snap target outside the slot is re-clamped into it")
    _assert(resolve_move_x(50, [], [], 6) is None,
            "no interval fits -> None")
    _assert(_close(resolve_move_x(22, [(0, 100)], [], 6, grid_x=uniform_grid(10)), 20),
            "snaps to the nearest beat grid line")


def test_resolve_resize_x():
    print("test_resolve_resize_x")
    _assert(_close(resolve_resize_x(50, 10, 40, [], 6), 40),
            "clamps the moving edge to max")
    _assert(_close(resolve_resize_x(5, 10, 40, [], 6), 10),
            "clamps the moving edge to min")
    # Resize does NOT re-clamp after snapping (unlike MOVE) — a snap target
    # may pull the edge slightly past the bound.
    _assert(_close(resolve_resize_x(38, 0, 40, [44], 8), 44),
            "snap may pull the edge past the bound (no re-clamp)")


def test_compute_bounds_middle_clip():
    print("test_compute_bounds_middle_clip")
    occ = [Occupant(10, False), Occupant(30, False), Occupant(50, False)]
    b = compute_bounds(occ, 1, song_end_time=100, pps=1.0, min_event_duration=MIN_DUR)
    _assert(isinstance(b, ClipBounds), "returns a ClipBounds")
    # Wedged between A's slot and C: width 20 only fits the (30,50) slot.
    _assert(len(b.move_intervals) == 1 and _close(b.move_intervals[0][0], 30)
            and _close(b.move_intervals[0][1], 30),
            "middle clip can only sit at 30 (move_intervals == [(30,30)])")
    _assert(_close(b.resize_left_min_x, 10 + MIN_DUR), "left edge min = A.end + MIN")
    _assert(_close(b.resize_left_max_x, 50 - MIN_DUR), "left edge max = right - MIN")
    _assert(_close(b.resize_right_min_x, 30 + MIN_DUR), "right edge min = start + MIN")
    _assert(_close(b.resize_right_max_x, 50), "right edge max = next clip start")
    _assert(b.snap_xs == [10.0, 30.0, 50.0, 100.0, 0.0, 100.0],
            "snap targets are the other units' edges + 0 + song end")


def test_compute_bounds_last_clip_moves_freely():
    print("test_compute_bounds_last_clip_moves_freely")
    occ = [Occupant(10, False), Occupant(30, False), Occupant(50, False)]
    b = compute_bounds(occ, 2, song_end_time=100, pps=1.0, min_event_duration=MIN_DUR)
    # Last clip is point-sized for collisions, so it has slots on both sides.
    # Each interval's upper bound is slot_end - width (width == MIN here).
    _assert(len(b.move_intervals) == 2, "last clip has two free slots")
    _assert(_close(b.move_intervals[0][0], 0)
            and _close(b.move_intervals[0][1], 10 - MIN_DUR),
            "first slot is (0, 10 - MIN)")


def test_compute_bounds_blackout_absorbed():
    print("test_compute_bounds_blackout_absorbed")
    # A clip, a BLACKOUT gap, then another clip. The BLACKOUT is not an
    # obstacle: A can move and resize across it.
    occ = [Occupant(10, False), Occupant(30, True), Occupant(50, False)]
    b = compute_bounds(occ, 0, song_end_time=100, pps=1.0, min_event_duration=MIN_DUR)
    _assert(len(b.move_intervals) == 1 and _close(b.move_intervals[0][0], 0)
            and _close(b.move_intervals[0][1], 30),
            "clip glides over the BLACKOUT (move slot 0..30)")
    _assert(_close(b.resize_right_max_x, 50),
            "right edge can extend through the BLACKOUT to the next real clip")


def main():
    test_zone_at()
    test_nearest_beat_x()
    test_snap_to_targets()
    test_clamp_to_intervals()
    test_resolve_move_x()
    test_resolve_resize_x()
    test_compute_bounds_middle_clip()
    test_compute_bounds_last_clip_moves_freely()
    test_compute_bounds_blackout_absorbed()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
