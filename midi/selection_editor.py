"""SelectionEditor — multi-selection batch move/delete.

Moves treat the selection as a rigid block; deletes reuse the
single-clip replace-with-BLACKOUT semantics behind one commit.
Operates on a shared MidiDocument + FadeEngine; commit is injected."""

from typing import Optional, List, Dict, Any, Tuple
import uuid

from tupperlogger import get_logger
from .constants import (
    MIN_EVENT_DURATION,
    BLACKOUT_EPSILON,
)


class SelectionEditor:
    def __init__(self, doc, fades, commit):
        self._doc = doc
        self._fades = fades
        self._commit = commit
        self.logger = get_logger("midi.selection_editor")

    def _selected_non_blackout(self, event_ids) -> List[Dict[str, Any]]:
        selected_ids = set(event_ids)
        return [e for e in self._doc.midi_events
                if e['id'] in selected_ids and not self._doc.is_blackout(e)]

    def selection_move_bounds(self, event_ids) -> Tuple[float, float]:
        """Allowed (min_delta, max_delta) seconds for a rigid-block move.

        For each selected non-BLACKOUT clip, leftward room is bounded by the
        right edge of the nearest non-selected non-BLACKOUT clip to its left
        (0.0 if none) and rightward room by the start of the nearest such clip
        to its right (song_end if none). Selected and BLACKOUT clips are never
        obstacles, so the block glides over gaps. Returns (0.0, 0.0) when the
        block cannot move.
        """
        selected_ids = set(event_ids)
        selected = self._selected_non_blackout(event_ids)
        if not selected:
            return (0.0, 0.0)
        song_end = self._doc._song_end_time()
        # Lower bound is the tightest (greatest) of the per-clip leftward
        # limits; upper bound is the tightest (least) rightward limit.
        min_delta = float('-inf')
        max_delta = float('inf')
        for e in selected:
            fixture = self._doc._event_fixture(e)
            idx = self._doc._index_of(e['id'])
            right = self._doc._right_edge_of(idx, fixture)
            width = (MIN_EVENT_DURATION if right == float('inf')
                     else max(MIN_EVENT_DURATION, right - e['time']))
            clip_right = e['time'] + width

            right_obstacle = song_end
            left_obstacle = 0.0
            for j, o in enumerate(self._doc.midi_events):
                if (self._doc._event_fixture(o) != fixture
                        or o['id'] in selected_ids
                        or self._doc.is_blackout(o)):
                    continue
                o_right = self._doc._right_edge_of(j, fixture)
                if o_right == float('inf'):
                    o_right = song_end
                if o['time'] >= clip_right - BLACKOUT_EPSILON:
                    right_obstacle = min(right_obstacle, o['time'])
                elif o_right <= e['time'] + BLACKOUT_EPSILON:
                    left_obstacle = max(left_obstacle, o_right)
            max_delta = min(max_delta, right_obstacle - clip_right)
            min_delta = max(min_delta, left_obstacle - e['time'])

        # Guard against float drift producing an inverted range.
        return (min(0.0, min_delta), max(0.0, max_delta))

    def _units_on_fixtures(self, fixtures) -> List[Dict[str, Any]]:
        """Render units (single events / scene groups) on the given fixtures.

        Each unit dict carries: fixture, group_id (or None), rep_id (a stable
        member event id) and key (group_id or rep_id). BLACKOUTs are excluded —
        they're the derived gap markers this rebuild reconstructs.
        """
        units: List[Dict[str, Any]] = []
        seen_groups = set()
        for e in self._doc.midi_events:
            fixture = self._doc._event_fixture(e)
            if fixture not in fixtures or self._doc.is_blackout(e):
                continue
            gid = e.get('group_id')
            if gid:
                if gid in seen_groups:
                    continue
                seen_groups.add(gid)
                units.append({'fixture': fixture, 'group_id': gid,
                              'rep_id': e['id'], 'key': gid})
            else:
                units.append({'fixture': fixture, 'group_id': None,
                              'rep_id': e['id'], 'key': e['id']})
        return units

    def _unit_is_selected(self, unit: Dict[str, Any], selected_ids) -> bool:
        if unit['group_id']:
            return any(m['id'] in selected_ids
                       for m in self._doc._group_members(unit['group_id']))
        return unit['rep_id'] in selected_ids

    def _unit_right_edge(self, unit: Dict[str, Any]) -> float:
        """Visual right edge — time of the next non-member same-fixture event."""
        if unit['group_id']:
            return self._doc._group_right_edge(unit['group_id'], unit['fixture'])
        idx = self._doc._index_of(unit['rep_id'])
        if idx < 0:
            return float('inf')
        return self._doc._right_edge_of(idx, unit['fixture'])

    def move_selection(self, event_ids, delta_sec: float) -> bool:
        """Slide every selected clip by the same delta, as a rigid block.

        Preserves the timing between selected clips. BLACKOUT gap markers on
        the affected fixtures are rebuilt from each clip's intended extent
        (the same technique move_event uses), reconstructing the vacated hole,
        internal block gaps, and boundary caps in one pass.
        """
        selected = self._selected_non_blackout(event_ids)
        if not selected:
            return False
        selected_ids = {e['id'] for e in selected}
        min_d, max_d = self.selection_move_bounds(event_ids)
        delta = max(min_d, min(delta_sec, max_d))
        if abs(delta) < BLACKOUT_EPSILON:
            return False

        fixtures = {self._doc._event_fixture(e) for e in selected}
        song_end = self._doc._song_end_time()

        # Snapshot each render UNIT's intended right edge per affected fixture.
        # A "unit" is a single event OR a whole scene group — the right
        # granularity, because a group's 7 members share one timestamp. Keying
        # by individual event would give each member a zero-width extent and,
        # after the post-shift re-sort, spray a BLACKOUT at the group's own
        # start (turning the LED bar off). Selected units carry their extent
        # with them (+= delta).
        units = self._units_on_fixtures(fixtures)
        intended = {}  # unit key -> (intended_end, unit)
        for u in units:
            u['selected'] = self._unit_is_selected(u, selected_ids)
            edge = self._unit_right_edge(u)
            if edge == float('inf'):
                edge = song_end
            if u['selected']:
                edge += delta
            intended[u['key']] = (edge, u)

        # Shift selected events (read original time before each single shift).
        for e in selected:
            self._doc._set_event_time_internal(e['id'], e['time'] + delta)

        # Drop every BLACKOUT on the affected fixtures; rebuild below.
        for fixture in fixtures:
            for bid in [e['id'] for e in self._doc.midi_events
                        if self._doc._event_fixture(e) == fixture
                        and self._doc.is_blackout(e)]:
                self._doc._delete_event_internal(bid)

        # Re-cap any unit whose post-move right edge over-extends past its
        # intended end. Decisions are computed against the now blackout-free
        # state first, then inserted together so a just-added cap can't
        # perturb a later unit's measurement.
        caps = []
        for _key, (intended_end, u) in intended.items():
            actual = self._unit_right_edge(u)
            if actual == float('inf'):
                actual = song_end
            if actual > intended_end + BLACKOUT_EPSILON:
                caps.append((intended_end, u['fixture']))
        for cap_time, fixture in caps:
            self._doc._insert_blackout_internal(cap_time, fixture)

        for gid in {e['group_id'] for e in selected if e.get('group_id')}:
            self._fades.regenerate_group(gid)
        for fixture in fixtures:
            self._doc.reconcile_blackouts(fixture)
        self._commit()
        self.logger.info(
            f"Moved selection of {len(selected)} events by {delta:.3f}s"
        )
        return True

    def delete_selection(self, event_ids) -> bool:
        """Delete a multi-selection behind one commit.

        Scene groups (detected via any selected member's group_id) are removed
        whole and replaced by a single BLACKOUT; single non-BLACKOUT clips are
        replaced in place with a BLACKOUT; already-BLACKOUT ids are removed.
        """
        selected_ids = set(event_ids)
        group_ids = set()
        singles: List[Dict[str, Any]] = []
        for e in self._doc.midi_events:
            if e['id'] not in selected_ids:
                continue
            gid = e.get('group_id')
            if gid:
                group_ids.add(gid)
            else:
                singles.append(e)
        if not group_ids and not singles:
            return False

        affected_fixtures = set()
        for gid in group_ids:
            members = self._doc._group_members(gid)
            if not members:
                continue
            fixture = self._doc._event_fixture(members[0])
            affected_fixtures.add(fixture)
            group_time = members[0]['time']
            self._fades.wipe_group(gid)
            for m in list(members):
                self._doc._delete_event_internal(m['id'])
            self._doc._insert_blackout_internal(group_time, fixture)

        for e in singles:
            affected_fixtures.add(self._doc._event_fixture(e))
            if self._doc.is_blackout(e):
                self._doc._delete_event_internal(e['id'])
            else:
                self._doc._replace_with_blackout_internal(e)

        for fixture in affected_fixtures:
            self._doc.reconcile_blackouts(fixture)
        self._commit()
        self.logger.info(
            f"Deleted selection: {len(group_ids)} scene(s), "
            f"{len(singles)} event(s)"
        )
        return True
