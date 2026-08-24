# ABOUTME: clip_geometry — pure, Qt-free geometry math for timeline clip drags.
# ABOUTME: zone detection, neighbour/beat snapping, interval clamping, bounds.
"""Pure geometry for clip-style timeline editing.

Everything here works in scene pixel coordinates (``time_seconds *
PIXELS_PER_SECOND``) and plain numbers — no Qt, no document — so the drag math
(the riskiest part of the timeline) can be unit-tested in isolation. The
``DraggableMidiEvent`` / ``MidiEventsTrack`` widgets are thin adapters that feed
these functions the zoom-adjusted pixel inputs and apply the results.
"""

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple, NamedTuple


# Drag modes (string-based for readability). Mirrored as class attributes on
# DraggableMidiEvent so existing `self.MODE_*` references keep working.
MODE_IDLE = "idle"
MODE_MOVE = "move"
MODE_RESIZE_LEFT = "resize_left"
MODE_RESIZE_RIGHT = "resize_right"
MODE_GROUP_MOVE = "group_move"  # rigid move of the whole multi-selection


@dataclass
class ClipBounds:
    """Pixel-space bounds and snap targets for a single clip's drag gestures.

    All values are in scene pixel coordinates (i.e. ``time_seconds *
    PIXELS_PER_SECOND``). The fields encode the policy of "no overlap",
    "BLACKOUTs absorb moves", and "preserve target width during MOVE".
    """
    # MOVE mode: list of (min_x, max_x) intervals where the clip's LEFT edge
    # may land. Multiple intervals enable "jumping" over other clips when a
    # large enough slot exists on the far side.
    move_intervals: List[Tuple[float, float]] = field(default_factory=list)
    # RESIZE_LEFT mode: bounds on the clip's LEFT edge (start time).
    resize_left_min_x: float = 0.0
    resize_left_max_x: float = 0.0
    # RESIZE_RIGHT mode: bounds on the clip's RIGHT edge (end time).
    resize_right_min_x: float = 0.0
    resize_right_max_x: float = 0.0
    # Snap targets — neighbor edges to which a dragged edge will snap.
    snap_xs: List[float] = field(default_factory=list)


class Occupant(NamedTuple):
    """A time-ordered track occupant used to compute a clip's drag bounds."""
    time: float
    is_blackout: bool


def zone_at(local_x: float, width: float, resize_zone: float,
            multi_selected: bool = False) -> str:
    """Classify a local x position into a drag zone.

    ``local_x`` and ``width`` are scene px (x measured from the clip's left
    edge); ``resize_zone`` is the edge-grab width in scene px. A multi-selected
    clip is all MOVE (no edge resize of several clips at once).
    """
    if multi_selected:
        return MODE_MOVE
    if width <= 2 * resize_zone:
        # Narrow clip: no MOVE zone; split half/half between resize zones.
        return MODE_RESIZE_LEFT if local_x < width / 2 else MODE_RESIZE_RIGHT
    if local_x <= resize_zone:
        return MODE_RESIZE_LEFT
    if local_x >= width - resize_zone:
        return MODE_RESIZE_RIGHT
    return MODE_MOVE


def nearest_beat_x(x: float, beat_px: float) -> float:
    """Scene-x of the beat grid line nearest to ``x`` (uniform grid from t=0)."""
    return round(x / beat_px) * beat_px


def uniform_grid(beat_px: float) -> Callable[[float], float]:
    """A ``grid_x`` callable for an evenly spaced grid — Time mode, and Bars
    mode on a song with no tempo changes."""
    return lambda x: nearest_beat_x(x, beat_px)


def snap_to_targets(x: float, candidates, radius: float) -> float:
    """Return the candidate within ``radius`` of ``x`` nearest to it, else ``x``."""
    nearest = None
    for tx in candidates:
        d = abs(tx - x)
        if d <= radius and (nearest is None or d < abs(nearest - x)):
            nearest = tx
    return nearest if nearest is not None else x


def clamp_to_intervals(proposed: float, intervals):
    """Clamp ``proposed`` into whichever (lo, hi) interval lands closest.

    Returns ``(clamped_x, (lo, hi))`` for the best interval, or ``None`` when
    there are no intervals (no slot fits the clip).
    """
    best = None
    best_distance = float('inf')
    for lo, hi in intervals:
        clamped = max(lo, min(proposed, hi))
        d = abs(clamped - proposed)
        if d < best_distance:
            best_distance = d
            best = (clamped, (lo, hi))
    return best


def resolve_move_x(proposed: float, intervals, snap_xs, snap_radius: float,
                   grid_x: Optional[Callable[[float], float]] = None
                   ) -> Optional[float]:
    """MOVE resolution → final left-edge x, or None when no interval fits.

    Clamp to the nearest valid interval, snap to neighbour edges / the nearest
    grid line, then re-clamp inside that interval so a snap target outside the
    slot can't push the clip into overlap.

    ``grid_x`` maps an x to the nearest grid line rather than naming a spacing:
    across a tempo change the spacing differs on either side of the cursor, so
    there is no single interval to round by.
    """
    best = clamp_to_intervals(proposed, intervals)
    if best is None:
        return None
    clamped, (lo, hi) = best
    candidates = list(snap_xs)
    if grid_x is not None:
        candidates.append(grid_x(clamped))
    snapped = snap_to_targets(clamped, candidates, snap_radius)
    return max(lo, min(snapped, hi))


def resolve_resize_x(proposed: float, min_x: float, max_x: float, snap_xs,
                     snap_radius: float,
                     grid_x: Optional[Callable[[float], float]] = None) -> float:
    """RESIZE resolution → final moving-edge x.

    Clamp the edge to ``[min_x, max_x]`` then snap. No re-clamp after snap —
    this matches the original behaviour where a snap target may pull the edge
    slightly past the bound.
    """
    clamped = max(min_x, min(proposed, max_x))
    candidates = list(snap_xs)
    if grid_x is not None:
        candidates.append(grid_x(clamped))
    return snap_to_targets(clamped, candidates, snap_radius)


def compute_bounds(occupants, idx: int, song_end_time: float, pps: float,
                   min_event_duration: float) -> ClipBounds:
    """Pixel-space ClipBounds for the time-ordered occupant at ``idx``.

    ``occupants`` is a time-ordered list of ``Occupant(time, is_blackout)``.
    Encodes the "no overlap / BLACKOUTs absorb moves / preserve width on MOVE"
    policy. A pure port of the former ``MidiEventsTrack._compute_bounds``.
    """
    n = len(occupants)
    cur_time = occupants[idx].time
    # cur_right is the unit's visual right edge (RESIZE_RIGHT bounds + snap
    # targets). cur_width_t is the *collision* width for MOVE bounds: the last
    # unit's visual extent stretches to song_end, but we treat it as
    # point-sized so it can move freely (matches manager.move_event).
    if idx + 1 < n:
        cur_right = occupants[idx + 1].time
        cur_width_t = max(min_event_duration, cur_right - cur_time)
    else:
        cur_right = song_end_time
        cur_width_t = min_event_duration

    # MOVE bounds: free-space slots wide enough to fit the current unit.
    obstacles: List[Tuple[float, float]] = []
    for j, u in enumerate(occupants):
        if j == idx or u.is_blackout:
            continue
        j_end = occupants[j + 1].time if j + 1 < n else song_end_time
        obstacles.append((u.time, j_end))
    obstacles.sort()

    slots: List[Tuple[float, float]] = []
    cursor = 0.0
    for ostart, oend in obstacles:
        if ostart > cursor:
            slots.append((cursor, ostart))
        cursor = max(cursor, oend)
    if cursor < song_end_time:
        slots.append((cursor, song_end_time))

    move_intervals: List[Tuple[float, float]] = []
    for slot_start, slot_end in slots:
        if slot_end - slot_start + 1e-6 < cur_width_t:
            continue
        move_intervals.append((slot_start * pps, (slot_end - cur_width_t) * pps))

    # RESIZE_LEFT — start can move toward the closest non-BLACKOUT predecessor.
    resize_left_min_t = 0.0
    for j in range(idx - 1, -1, -1):
        if not occupants[j].is_blackout:
            resize_left_min_t = occupants[j].time + min_event_duration
            break
    resize_left_max_t = cur_right - min_event_duration

    # RESIZE_RIGHT — right edge can move toward the closest non-BLACKOUT successor.
    resize_right_min_t = cur_time + min_event_duration
    resize_right_max_t = song_end_time
    for j in range(idx + 1, n):
        if not occupants[j].is_blackout:
            resize_right_max_t = occupants[j].time
            break

    # Snap targets: every other unit's start & end edges, plus 0 and song end.
    snap_xs: List[float] = []
    for j, u in enumerate(occupants):
        if j == idx:
            continue
        snap_xs.append(u.time * pps)
        j_right = occupants[j + 1].time if j + 1 < n else song_end_time
        snap_xs.append(j_right * pps)
    snap_xs.extend([0.0, song_end_time * pps])

    return ClipBounds(
        move_intervals=move_intervals,
        resize_left_min_x=resize_left_min_t * pps,
        resize_left_max_x=resize_left_max_t * pps,
        resize_right_min_x=resize_right_min_t * pps,
        resize_right_max_x=resize_right_max_t * pps,
        snap_xs=snap_xs,
    )
