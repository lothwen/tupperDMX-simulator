"""MidiDocument — the shared data model for the MIDI manager subsystem.

Owns the single source of truth (``midi_data``, ``midi_events``,
``midi_notes``, ``midi_note_map``, plus the externally-provided song-duration
hint) and the low-level queries, atomic mutators, and BLACKOUT invariant
enforcement that every editor collaborator builds on. No Qt, no worker, no
commit — pure data + integrity, which is what makes the editors unit-testable.

Method names are kept identical to their former homes on ``MidiManager`` so the
extraction is a faithful move; collaborators reach them via ``self._doc``.
"""

from typing import Optional, List, Dict, Any, Tuple
import uuid

import mido

from tupperlogger import get_logger
from .constants import (
    BLACKOUT_PART_NAME,
    MIN_EVENT_DURATION,
    BLACKOUT_EPSILON,
    NOTE_MATCH_TOLERANCE,
)


class MidiDocument:
    """Mutable MIDI editing state + integrity primitives."""

    def __init__(self, midi_note_map: Optional[Dict] = None,
                 fixture_channels: Optional[Dict[str, int]] = None):
        # MIDI Data Storage - Single Source of Truth
        self.midi_data: Optional[mido.MidiFile] = None
        self.midi_events: List[Dict[str, Any]] = []  # Events for UI display with unique IDs
        # note_on/note_off pairs (with duration) used to rebuild the MIDI file on save
        self.midi_notes: List[Dict[str, Any]] = []
        self.midi_note_map = midi_note_map or {}

        # fixture-type name -> the MIDI channel (= fixture instance index in the
        # patch) new events of that type route to. Set from config.fixtures in
        # the real app; left None for synthetic/test maps, where events carry no
        # channel and fixture identity falls back to the note map's type hint.
        self.fixture_channels = fixture_channels or {}

        # Externally-provided song duration (e.g. the audio file's length).
        # Used as a floor when computing the song-end clamp so clip-edit
        # operations have a sensible range even when the loaded MIDI is
        # sparse — see song_end_time().
        self._song_duration_override: float = 0.0

        self.logger = get_logger("midi.document")

    # --- song duration -------------------------------------------------------

    def set_song_duration(self, duration: float) -> None:
        """Hint at the song's actual end time (e.g. the audio file's length).

        Used as a floor for song_end_time() so clip-edit bounds aren't
        pinned to the (potentially tiny) MIDI duration after the first
        clip is dropped on an otherwise-empty track.
        """
        self._song_duration_override = max(0.0, float(duration))

    def song_end_time(self) -> float:
        """Effective end-of-timeline for clip placement and visual extents.

        Returns the maximum of:
          * the loaded MIDI's actual length;
          * any externally-provided audio duration (set_song_duration);
          * a 240s default fallback.
        """
        midi_length = self.midi_data.length if self.midi_data else 0.0
        return max(midi_length, self._song_duration_override, 240.0)

    def _song_end_time(self) -> float:
        """Backwards-compatible alias for song_end_time()."""
        return self.song_end_time()

    # --- queries -------------------------------------------------------------

    def is_blackout(self, event: Dict[str, Any]) -> bool:
        """Return True if `event` is any BLACKOUT* gap marker (prefix match)."""
        info = event.get('info') if event else None
        if info is None:
            return False
        part = getattr(info, 'part', '') or ''
        return part.upper().startswith(BLACKOUT_PART_NAME)

    def _event_fixture(self, event: Dict[str, Any]):
        """Scoping key for the fixture an event belongs to.

        Real-app events carry a MIDI channel (= the fixture instance's index in
        the patch), which scopes each fixture independently. Synthetic / legacy
        events without a channel fall back to the note map's fixture-type hint
        ('ledbar', 'blinder', ...). Either way the value is only ever compared
        against other _event_fixture() results, so the int/str mix is internal.
        """
        if event:
            ch = event.get('channel')
            if ch is not None:
                return ch
        info = event.get('info') if event else None
        return getattr(info, 'fixture', None) if info else None

    def _channel_for_note(self, note: int) -> Optional[int]:
        """Channel a new event of `note` routes to: the patch channel for the
        note's fixture type. None when no patch is configured (tests) or the
        type isn't patched, leaving the event channel-less (type-scoped)."""
        if not self.fixture_channels:
            return None
        info = self.midi_note_map.get(str(note))
        ftype = getattr(info, 'fixture', None) if info else None
        return self.fixture_channels.get(ftype)

    def _blackout_note_number(self, fixture=None) -> Optional[int]:
        """Return the MIDI note number mapped to BLACKOUT* for `fixture`.

        The generalized map has a single BLACKOUT verb (the channel selects the
        fixture), so when `fixture` is a channel index (int) — or absent — the
        sole blackout note is returned. Legacy / synthetic maps with one
        blackout note per fixture *type* still match by the type string.
        """
        matches = [
            note_str for note_str, info in self.midi_note_map.items()
            if getattr(info, 'part', '').upper().startswith(BLACKOUT_PART_NAME)
        ]
        if not matches:
            return None

        # Channel-scoped (real app) or unscoped: the channel disambiguates, so
        # the single blackout verb applies to every fixture.
        if not isinstance(fixture, str):
            try:
                return int(matches[0])
            except (TypeError, ValueError):
                return None

        # Type-scoped (legacy/synthetic): pick the blackout note for that type.
        for note_str in matches:
            if getattr(self.midi_note_map[note_str], 'fixture', None) == fixture:
                try:
                    return int(note_str)
                except (TypeError, ValueError):
                    continue
        return None

    def _index_of(self, event_id: str) -> int:
        for i, e in enumerate(self.midi_events):
            if e['id'] == event_id:
                return i
        return -1

    def _right_edge_of(self, idx: int, fixture: Optional[str] = None) -> float:
        """Visual right edge of event at index idx.

        If `fixture` is given, returns the time of the next event of the
        same fixture (so each track is scoped independently); otherwise
        falls back to the next event globally.
        """
        if fixture is None:
            if 0 <= idx < len(self.midi_events) - 1:
                return self.midi_events[idx + 1]['time']
            return float('inf')
        for j in range(idx + 1, len(self.midi_events)):
            if self._event_fixture(self.midi_events[j]) == fixture:
                return self.midi_events[j]['time']
        return float('inf')

    def _non_blackout_intervals(self, exclude_event_id: Optional[str] = None,
                                fixture: Optional[str] = None
                                ) -> List[Tuple[float, float, str]]:
        """Visible intervals of non-BLACKOUT clips, sorted by start.

        Each clip's interval = [clip.time, next_event.time). The last event's
        interval extends to song_end. Excludes `exclude_event_id` if given.
        If `fixture` is given, only events of that fixture are considered and
        each clip's right edge becomes the next *same-fixture* event.
        """
        song_end = self._song_end_time()
        candidates: List[Dict[str, Any]] = (
            [e for e in self.midi_events if self._event_fixture(e) == fixture]
            if fixture is not None else self.midi_events
        )
        intervals: List[Tuple[float, float, str]] = []
        for i, e in enumerate(candidates):
            if self.is_blackout(e) or e['id'] == exclude_event_id:
                continue
            end = (candidates[i + 1]['time']
                   if i + 1 < len(candidates) else song_end)
            intervals.append((e['time'], end, e['id']))
        intervals.sort(key=lambda x: x[0])
        return intervals

    def _free_slots_for_move(self, exclude_event_id: str,
                             fixture: Optional[str] = None
                             ) -> List[Tuple[float, float]]:
        """Compute (start, end) intervals where a clip can be placed without
        overlapping any non-BLACKOUT clip of the same fixture (excluding
        `exclude_event_id`)."""
        obstacles = self._non_blackout_intervals(exclude_event_id, fixture)
        slots: List[Tuple[float, float]] = []
        cursor = 0.0
        for start, end, _ in obstacles:
            if start > cursor:
                slots.append((cursor, start))
            cursor = max(cursor, end)
        # Tail slot extends past the last obstacle to the song end.
        song_end = self._song_end_time()
        if cursor < song_end:
            slots.append((cursor, song_end))
        return slots

    def _group_members(self, group_id: str) -> List[Dict[str, Any]]:
        return [e for e in self.midi_events if e.get("group_id") == group_id]

    def _group_right_edge(self, group_id: str,
                          fixture: Optional[str]) -> float:
        """Time of the first non-member same-fixture event after the group."""
        members = self._group_members(group_id)
        if not members:
            return float('inf')
        group_time = members[0]["time"]
        member_ids = {e["id"] for e in members}
        nearest = float('inf')
        for e in self.midi_events:
            if (self._event_fixture(e) == fixture
                    and e["id"] not in member_ids
                    and e["time"] > group_time + BLACKOUT_EPSILON
                    and e["time"] < nearest):
                nearest = e["time"]
        return nearest

    def _free_slots_for_group_move(self, group_id: str,
                                   fixture: Optional[str]
                                   ) -> List[Tuple[float, float]]:
        """Slots where the group can land. All group members excluded as obstacles."""
        member_ids = {e["id"] for e in self._group_members(group_id)}
        candidates = [e for e in self.midi_events
                      if self._event_fixture(e) == fixture]
        obstacles: List[Tuple[float, float]] = []
        for i, e in enumerate(candidates):
            if self.is_blackout(e) or e["id"] in member_ids:
                continue
            end = float('inf')
            for j in range(i + 1, len(candidates)):
                if candidates[j]["id"] in member_ids:
                    continue
                end = candidates[j]["time"]
                break
            if end == float('inf'):
                end = self._song_end_time()
            obstacles.append((e["time"], end))
        obstacles.sort(key=lambda x: x[0])

        slots: List[Tuple[float, float]] = []
        cursor = 0.0
        for start, end in obstacles:
            if start > cursor:
                slots.append((cursor, start))
            cursor = max(cursor, end)
        song_end = self._song_end_time()
        if cursor < song_end:
            slots.append((cursor, song_end))
        return slots

    def _visible_duration_for_event(self, event_id: str) -> float:
        """Visible width of a clip in seconds (next same-fixture event - this)."""
        idx = self._index_of(event_id)
        if idx < 0:
            return 0.0
        fixture = self._event_fixture(self.midi_events[idx])
        right = self._right_edge_of(idx, fixture)
        if right == float('inf'):
            right = self._song_end_time()
        return max(0.0, right - self.midi_events[idx]['time'])

    def _visible_duration_for_group(self, group_id: str) -> float:
        members = self._group_members(group_id)
        if not members:
            return 0.0
        fixture = self._event_fixture(members[0])
        right = self._group_right_edge(group_id, fixture)
        if right == float('inf'):
            right = self._song_end_time()
        return max(0.0, right - members[0]['time'])

    # --- atomic mutators (no rebuild / no signal emit) -----------------------

    def _insert_blackout_internal(self, time: float,
                                  fixture=None) -> Optional[str]:
        """Insert a BLACKOUT event+note at `time`. No rebuild/emit. Returns new id, or None."""
        bo_note = self._blackout_note_number(fixture)
        if bo_note is None:
            self.logger.warning(
                f"Cannot auto-insert BLACKOUT for fixture={fixture!r}: "
                "no matching entry in midi_note_map"
            )
            return None

        # `fixture` is the scoping token from _event_fixture: a channel index
        # (real app) or a type string (synthetic). Only the int form is a real
        # MIDI channel to stamp on the gap marker.
        channel = fixture if isinstance(fixture, int) else None

        event_id = str(uuid.uuid4())
        info = self.midi_note_map[str(bo_note)]
        self.midi_events.append({
            "id": event_id,
            "note": bo_note,
            "time": time,
            "velocity": 100,
            "channel": channel,
            "info": info,
            "group_id": None,
            "scene_name": None,
        })
        self.midi_events.sort(key=lambda x: x['time'])

        note_id = str(uuid.uuid4())
        self.midi_notes.append({
            "id": note_id,
            "note": bo_note,
            "time": time,
            "velocity": 100,
            "duration": 0.1,
            "channel": channel,
        })
        self.midi_notes.sort(key=lambda x: x['time'])
        return event_id

    def _delete_event_internal(self, event_id: str) -> bool:
        """Remove event AND its corresponding note. No rebuild/emit."""
        event = None
        for i, e in enumerate(self.midi_events):
            if e['id'] == event_id:
                event = self.midi_events.pop(i)
                break
        if event is None:
            return False
        # Skip is_fade entries — those are owned by their parent scene group,
        # not by this event, even when their (note, time) coincide with it.
        for i, note in enumerate(self.midi_notes):
            if note.get('is_fade'):
                continue
            if (note['note'] == event['note']
                    and note.get('channel') == event.get('channel')
                    and abs(note['time'] - event['time']) < NOTE_MATCH_TOLERANCE):
                self.midi_notes.pop(i)
                break
        return True

    def _set_event_time_internal(self, event_id: str, new_time: float) -> bool:
        """Set an event's time and the matching note's time. No rebuild/emit."""
        for e in self.midi_events:
            if e['id'] == event_id:
                old_time = e['time']
                e['time'] = new_time
                # Mirror in midi_notes (matched by note + old time). Skip
                # fade notes — they are regenerated separately by the caller.
                for note in self.midi_notes:
                    if note.get('is_fade'):
                        continue
                    if (note['note'] == e['note']
                            and note.get('channel') == e.get('channel')
                            and abs(note['time'] - old_time) < NOTE_MATCH_TOLERANCE):
                        note['time'] = new_time
                        break
                self.midi_events.sort(key=lambda x: x['time'])
                self.midi_notes.sort(key=lambda x: x['time'])
                return True
        return False

    def _add_event_internal(self, note: int, time: float, velocity: int,
                            *, group_id: Optional[str] = None,
                            scene_name: Optional[str] = None,
                            channel: Optional[int] = None) -> Optional[str]:
        """Insert one event+note without rebuild/commit. Returns event_id."""
        if str(note) not in self.midi_note_map:
            return None
        # Route a new event to its fixture type's patch channel (None when no
        # patch is configured, i.e. type-scoped synthetic data) unless the
        # caller targets a specific fixture instance (its patch index).
        if channel is None:
            channel = self._channel_for_note(note)
        event_id = str(uuid.uuid4())
        self.midi_events.append({
            "id": event_id,
            "note": note,
            "time": time,
            "velocity": velocity,
            "channel": channel,
            "info": self.midi_note_map[str(note)],
            "group_id": group_id,
            "scene_name": scene_name,
            "fade_in": 0.0,
            "fade_out": 0.0,
        })
        self.midi_events.sort(key=lambda x: x["time"])

        self.midi_notes.append({
            "id": str(uuid.uuid4()),
            "note": note,
            "time": time,
            "velocity": velocity,
            "duration": 0.1,
            "channel": channel,
        })
        self.midi_notes.sort(key=lambda x: x["time"])
        return event_id

    def _replace_with_blackout_internal(self, event: Dict[str, Any]) -> bool:
        """Swap an event's note to its fixture's BLACKOUT note in place.

        No reconcile/rebuild/commit — the caller batches those. Returns False
        when the event is already a BLACKOUT or its fixture has no BLACKOUT
        mapping (same no-op semantics as replace_with_blackout()).
        """
        if self.is_blackout(event):
            return False
        fixture = self._event_fixture(event)
        bo_note = self._blackout_note_number(fixture)
        if bo_note is None:
            self.logger.warning(
                f"Cannot replace event {event['id']} with BLACKOUT: "
                f"no BLACKOUT note mapped for fixture={fixture!r}"
            )
            return False
        old_note = event['note']
        old_time = event['time']
        event['note'] = bo_note
        event['info'] = self.midi_note_map[str(bo_note)]
        for note in self.midi_notes:
            if note.get('is_fade'):
                continue
            if (note['note'] == old_note
                    and note.get('channel') == event.get('channel')
                    and abs(note['time'] - old_time) < NOTE_MATCH_TOLERANCE):
                note['note'] = bo_note
                break
        return True

    # --- integrity -----------------------------------------------------------

    def reconcile_blackouts(self, fixture: Optional[str] = None) -> None:
        """Enforce BLACKOUT invariants per fixture. Idempotent.

        BLACKOUTs are scoped to their own fixture: a blinder BLACKOUT
        sandwiched between two ledbar events should NOT be merged or
        dropped based on those neighbors. When `fixture` is None, every
        fixture present in the data is reconciled in turn.
        """
        if fixture is None:
            fixtures = {self._event_fixture(e) for e in self.midi_events}
            fixtures.discard(None)
            for f in fixtures:
                self.reconcile_blackouts(f)
            return

        # Sort first
        self.midi_events.sort(key=lambda x: x['time'])
        self.midi_notes.sort(key=lambda x: x['time'])

        # Pass 1: drop zero/negative-duration same-fixture BLACKOUTs.
        # A BLACKOUT's effective duration = (next same-fixture event's time) - (its time).
        # If <= BLACKOUT_EPSILON, drop it.
        dropped = True
        while dropped:
            dropped = False
            filtered = [e for e in self.midi_events
                        if self._event_fixture(e) == fixture]
            for i, e in enumerate(filtered):
                if not self.is_blackout(e):
                    continue
                next_time = (filtered[i + 1]['time']
                             if i + 1 < len(filtered) else float('inf'))
                if next_time - e['time'] <= BLACKOUT_EPSILON:
                    self._delete_event_internal(e['id'])
                    dropped = True
                    break  # restart loop after mutation

        # Pass 2: merge adjacent same-fixture BLACKOUTs (delete the later one).
        merged = True
        while merged:
            merged = False
            filtered = [e for e in self.midi_events
                        if self._event_fixture(e) == fixture]
            for i in range(len(filtered) - 1):
                if self.is_blackout(filtered[i]) and self.is_blackout(filtered[i + 1]):
                    self._delete_event_internal(filtered[i + 1]['id'])
                    merged = True
                    break
