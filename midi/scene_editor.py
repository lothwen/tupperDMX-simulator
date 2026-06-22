"""SceneEditor — scene-group ops, group reattach, and scene-name lookup.

A scene is N simultaneous channel-control note_on events on ONE fixture (notes
1..channel_count, the fixture's channels in order). They share a `group_id`
while in memory; on file save the group_id is dropped and only the same-time
note_ons remain — reattach_groups() rebuilds the group from that same-time,
same-channel clustering on the next load (group_ids are ephemeral). The exact
channel<->note mapping and the fixture's scene library are resolved per fixture
via the document (the MIDI channel selects the fixture).

Operates on a shared MidiDocument and a FadeEngine (groups regenerate their
fade ramps on move/resize/update); commit is injected."""

from typing import Optional, List, Dict, Any, Tuple
import uuid

from tupperlogger import get_logger
from software_controller.scene_loader import Scene, load_scenes
from .constants import (
    MIN_EVENT_DURATION,
    BLACKOUT_EPSILON,
    NOTE_MATCH_TOLERANCE,
    _channel_to_velocity,
)


class SceneEditor:
    def __init__(self, doc, fades, commit):
        self._doc = doc
        self._fades = fades
        self._commit = commit
        # Lazily-loaded scene library, cached per fixture type.
        self._scene_cache: Dict[str, List[Scene]] = {}
        self.logger = get_logger("midi.scene_editor")

    def _fixture_type_for(self, channel) -> str:
        """Fixture-type name patched at `channel`; defaults to ledbar when no
        patch resolves (synthetic data / tests)."""
        fdef = self._doc._fdef_for_channel(channel)
        return fdef.name if fdef else "ledbar"

    def _get_scenes(self, fixture_type: str) -> List[Scene]:
        """Return the cached scene library for a fixture type."""
        if fixture_type not in self._scene_cache:
            try:
                self._scene_cache[fixture_type] = load_scenes(fixture_type)
            except Exception as exc:
                self.logger.warning(
                    f"Could not load scene library for '{fixture_type}': {exc}"
                )
                self._scene_cache[fixture_type] = []
        return self._scene_cache[fixture_type]

    def _lookup_scene_name(self, cluster: List[Dict[str, Any]]) -> Optional[str]:
        """Match a freshly-clustered scene against the fixture's scene library.

        Comparison happens in MIDI-velocity space (the same lossy domain the
        scene values live in once embedded in a MIDI file), so 254/255 raw
        channel values both collapse to velocity 127 and still match.
        Returns the matching scene's name, or None if no exact match exists.
        """
        channel = cluster[0].get("channel")
        field_note = self._doc.channel_note_map(channel)
        note_to_field = {note: field for field, note in field_note}
        cluster_vels: Dict[str, int] = {}
        for ev in cluster:
            field = note_to_field.get(ev["note"])
            if field is None:
                continue
            cluster_vels[field] = int(ev["velocity"])
        # Need every channel present for an unambiguous match.
        if len(cluster_vels) != len(field_note):
            return None
        for scene in self._get_scenes(self._fixture_type_for(channel)):
            scene_vels = {f: _channel_to_velocity(v)
                          for f, v in scene.values.items()}
            if scene_vels == cluster_vels:
                return scene.name
        return None

    def reattach_groups(self) -> None:
        """Cluster channel-control events at the same time into scene groups.

        Heuristic: any 2+ events at the same time (within BLACKOUT_EPSILON) on
        the same MIDI channel whose note drives one of that fixture's channels
        form one scene clip. Existing group_id assignments are preserved
        (idempotent re-entry).
        """
        self._doc.midi_events.sort(key=lambda x: x["time"])
        i = 0
        while i < len(self._doc.midi_events):
            e = self._doc.midi_events[i]
            channel = e.get("channel")
            if (not self._doc.is_channel_control_note(e["note"], channel)
                    or e.get("group_id")):
                i += 1
                continue
            cluster = [e]
            j = i + 1
            while j < len(self._doc.midi_events):
                f = self._doc.midi_events[j]
                if abs(f["time"] - e["time"]) > BLACKOUT_EPSILON:
                    break
                if (f.get("channel") == channel
                        and self._doc.is_channel_control_note(f["note"], channel)
                        and not f.get("group_id")):
                    cluster.append(f)
                j += 1
            if len(cluster) >= 2:
                gid = str(uuid.uuid4())
                # Prefer the real name from the LED scene library; fall back
                # to a time-based label for partial or hand-edited clusters
                # that don't match any library entry.
                scene_name = (self._lookup_scene_name(cluster)
                              or f"Scene @ {e['time']:.2f}s")
                for c in cluster:
                    c["group_id"] = gid
                    c["scene_name"] = scene_name
            i += 1

    def add_scene(self, time: float, channels: Dict[str, int],
                  name: str, channel: Optional[int] = None) -> Optional[str]:
        """Create a 7-event LED scene group at `time`. Returns group_id or None.

        `channel` targets a specific fixture instance (its patch index); when
        None each event routes to its type's patch channel as usual.
        """
        group_id = str(uuid.uuid4())
        added = 0
        for ch_name, note in self._doc.channel_note_map(channel):
            if ch_name not in channels:
                continue
            velocity = _channel_to_velocity(channels[ch_name])
            if self._doc._add_event_internal(note, time, velocity,
                                        group_id=group_id,
                                        scene_name=name,
                                        channel=channel) is not None:
                added += 1
        if added == 0:
            self.logger.warning(
                f"add_scene at {time:.3f}s produced 0 events "
                "(channel-control notes missing from midi_note_map?)"
            )
            return None
        # Reconcile in case the scene landed on top of a cap-BLACKOUT;
        # otherwise reconcile would have to wait for a later mutation to
        # clear the now-zero-duration BLACKOUT.
        members = self._doc._group_members(group_id)
        if members:
            self._doc.reconcile_blackouts(self._doc._event_fixture(members[0]))
        self._commit()
        self.logger.info(
            f"Added scene '{name}' at {time:.3f}s ({added} channels, "
            f"group_id={group_id})"
        )
        return group_id

    def move_scene(self, group_id: str, new_time: float) -> bool:
        """Move all events sharing group_id to new_time atomically."""
        members = self._doc._group_members(group_id)
        if not members:
            return False
        fixture = self._doc._event_fixture(members[0])
        old_time = members[0]["time"]
        member_ids = {e["id"] for e in members}

        target_old_right = self._doc._group_right_edge(group_id, fixture)
        song_end = self._doc._song_end_time()
        # When nothing follows the group, target width is just the minimum —
        # the group can move freely. Otherwise preserve the clip's visible
        # width so it doesn't overlap its successor after the move.
        if target_old_right == float('inf'):
            target_width = MIN_EVENT_DURATION
        else:
            target_width = max(MIN_EVENT_DURATION, target_old_right - old_time)
        if new_time < 0:
            new_time = 0.0

        # Snapshot intended ends for OTHER same-fixture non-BLACKOUT clips.
        intended_ends: Dict[str, float] = {}
        for i, e in enumerate(self._doc.midi_events):
            if (self._doc._event_fixture(e) != fixture
                    or self._doc.is_blackout(e)
                    or e.get("group_id") == group_id):
                continue
            end = self._doc._right_edge_of(i, fixture)
            if end == float('inf'):
                end = song_end
            intended_ends[e["id"]] = end

        slots = self._doc._free_slots_for_group_move(group_id, fixture)
        best_new_time: Optional[float] = None
        best_distance = float('inf')
        for slot_start, slot_end in slots:
            if slot_end - slot_start + BLACKOUT_EPSILON < target_width:
                continue
            clamped = max(slot_start, min(new_time, slot_end - target_width))
            d = abs(clamped - new_time)
            if d < best_distance:
                best_distance = d
                best_new_time = clamped
        if best_new_time is None:
            self.logger.debug(
                f"move_scene {group_id}: no slot fits target width "
                f"{target_width:.3f}s"
            )
            return False
        if abs(best_new_time - old_time) < BLACKOUT_EPSILON:
            return False

        new_target_end = best_new_time + target_width

        # Capture the group's old trailing cap (if any) BEFORE the move so
        # it travels with the clip — same reason as move_event. We look for
        # the first non-member same-fixture event after the group's start.
        old_trailing_cap_id: Optional[str] = None
        sorted_after: List[Dict[str, Any]] = sorted(
            (e for e in self._doc.midi_events
             if self._doc._event_fixture(e) == fixture
             and e["id"] not in member_ids
             and e["time"] > old_time + BLACKOUT_EPSILON),
            key=lambda e: e["time"],
        )
        if sorted_after and self._doc.is_blackout(sorted_after[0]):
            old_trailing_cap_id = sorted_after[0]["id"]

        for eid in member_ids:
            self._doc._set_event_time_internal(eid, best_new_time)

        # Drop stale BLACKOUTs inside the group's new visible span (start
        # inclusive — same reason as move_event: a same-time BLACKOUT at
        # the new position would otherwise collapse the visible width).
        stale = [e["id"] for e in self._doc.midi_events
                 if e["id"] not in member_ids
                 and self._doc._event_fixture(e) == fixture
                 and self._doc.is_blackout(e)
                 and best_new_time - BLACKOUT_EPSILON <= e["time"]
                 < new_target_end - BLACKOUT_EPSILON]
        if old_trailing_cap_id is not None and old_trailing_cap_id not in stale:
            stale.append(old_trailing_cap_id)
        for bid in stale:
            self._doc._delete_event_internal(bid)

        # Restore intended ends for other same-fixture clips.
        for eid, intended_end in intended_ends.items():
            idx = self._doc._index_of(eid)
            if idx < 0:
                continue
            actual_end = self._doc._right_edge_of(idx, fixture)
            if actual_end == float('inf'):
                actual_end = song_end
            if actual_end > intended_end + BLACKOUT_EPSILON:
                self._doc._insert_blackout_internal(intended_end, fixture)

        # Re-cap the group itself: when the group had a trailing cap before
        # the move, that cap was deleted (it travels with the clip). The
        # equivalent of move_event's `intended_ends[event_id] override`
        # lives here — group members are excluded from `intended_ends`,
        # so we have to insert the new cap explicitly. We use
        # _group_right_edge to look at non-member same-fixture neighbors.
        if target_old_right != float('inf'):
            group_actual_end = self._doc._group_right_edge(group_id, fixture)
            if group_actual_end == float('inf'):
                group_actual_end = song_end
            if group_actual_end > new_target_end + BLACKOUT_EPSILON:
                self._doc._insert_blackout_internal(new_target_end, fixture)

        self._fades.regenerate_group(group_id)
        self._doc.reconcile_blackouts(fixture)
        self._commit()
        self.logger.info(
            f"Moved scene {group_id} from {old_time:.3f}s to "
            f"{best_new_time:.3f}s"
        )
        return True

    def update_scene(self, group_id: str, channels: Dict[str, int],
                     name: Optional[str] = None) -> bool:
        """Update channel velocities (and optionally name) of a scene group.

        Time and group membership stay unchanged. For each known channel in
        `channels`, the matching event's velocity is set to channel_value//2.
        Channels missing from the dict are left alone.
        """
        members = self._doc._group_members(group_id)
        if not members:
            return False
        members_by_note = {m["note"]: m for m in members}

        for ch_name, note in self._doc.channel_note_map(members[0].get("channel")):
            if ch_name not in channels:
                continue
            evt = members_by_note.get(note)
            if evt is None:
                continue
            new_vel = _channel_to_velocity(channels[ch_name])
            evt["velocity"] = new_vel
            # Mirror in midi_notes (matched by note + time). Skip is_fade
            # entries — they belong to the group's fade ramp, not the
            # original channel-control event.
            for n in self._doc.midi_notes:
                if n.get("is_fade"):
                    continue
                if (n["note"] == note
                        and abs(n["time"] - evt["time"]) < NOTE_MATCH_TOLERANCE):
                    n["velocity"] = new_vel
                    break

        if name is not None:
            for m in members:
                m["scene_name"] = name

        # Brightness V may have changed → fade ramp endpoint changes.
        self._fades.regenerate_group(group_id)
        self._commit()
        self.logger.info(
            f"Updated scene {group_id}"
            + (f" (renamed to '{name}')" if name is not None else "")
        )
        return True

    def delete_scene(self, group_id: str) -> bool:
        """Remove all members of the group; insert a single BLACKOUT at group_time."""
        members = self._doc._group_members(group_id)
        if not members:
            return False
        fixture = self._doc._event_fixture(members[0])
        group_time = members[0]["time"]
        # Wipe fade notes BEFORE deleting the members; once members are gone
        # _regenerate_group_fades wouldn't know what to wipe (no group anchor).
        self._fades.wipe_group(group_id)
        for e in list(members):
            self._doc._delete_event_internal(e["id"])
        self._doc._insert_blackout_internal(group_time, fixture)
        self._doc.reconcile_blackouts(fixture)
        self._commit()
        self.logger.info(f"Deleted scene {group_id} (replaced with BLACKOUT)")
        return True

    def set_scene_left_edge(self, group_id: str, new_time: float) -> bool:
        """Resize a scene from the left — shifts all members' time together."""
        members = self._doc._group_members(group_id)
        if not members:
            return False
        fixture = self._doc._event_fixture(members[0])
        old_time = members[0]["time"]
        member_ids = {e["id"] for e in members}

        right_edge = self._doc._group_right_edge(group_id, fixture)
        max_new_time = (right_edge - MIN_EVENT_DURATION
                        if right_edge != float('inf') else float('inf'))

        # Walk back through same-fixture non-member events sorted by time.
        prev_candidates = sorted(
            (e for e in self._doc.midi_events
             if self._doc._event_fixture(e) == fixture
             and e["id"] not in member_ids
             and e["time"] < old_time - BLACKOUT_EPSILON),
            key=lambda e: e["time"],
        )
        prev_same: Optional[Dict[str, Any]] = (
            prev_candidates[-1] if prev_candidates else None
        )
        min_new_time = 0.0
        for e in reversed(prev_candidates):
            if not self._doc.is_blackout(e):
                min_new_time = e["time"] + MIN_EVENT_DURATION
                break

        new_time = max(min_new_time, min(new_time, max_new_time))
        delta = new_time - old_time
        if abs(delta) < BLACKOUT_EPSILON:
            return False

        prev_was_blackout = (prev_same is not None
                             and self._doc.is_blackout(prev_same))

        for eid in member_ids:
            self._doc._set_event_time_internal(eid, new_time)

        # If we shrunk and the immediate predecessor is non-BLACKOUT, fill the
        # exposed gap with a fixture-matching BLACKOUT.
        if delta > 0 and not prev_was_blackout and prev_same is not None:
            self._doc._insert_blackout_internal(old_time, fixture)

        self._fades.clamp_group_to_visible(group_id)
        self._fades.regenerate_group(group_id)
        self._doc.reconcile_blackouts(fixture)
        self._commit()
        self.logger.info(
            f"Resized scene {group_id} left edge {old_time:.3f}s → "
            f"{new_time:.3f}s"
        )
        return True

    def set_scene_right_edge(self, group_id: str,
                             new_right_time: float) -> bool:
        """Resize a scene from the right — moves/inserts the trailing BLACKOUT."""
        members = self._doc._group_members(group_id)
        if not members:
            return False
        fixture = self._doc._event_fixture(members[0])
        group_time = members[0]["time"]
        member_ids = {e["id"] for e in members}

        min_right = group_time + MIN_EVENT_DURATION

        # Find the next non-member same-fixture non-BLACKOUT (caps the right edge)
        # and the next non-member same-fixture event of any kind (the gap marker
        # we may need to move).
        max_right = float('inf')
        next_event: Optional[Dict[str, Any]] = None
        for e in self._doc.midi_events:
            if (self._doc._event_fixture(e) != fixture
                    or e["id"] in member_ids
                    or e["time"] <= group_time + BLACKOUT_EPSILON):
                continue
            if next_event is None:
                next_event = e
            if not self._doc.is_blackout(e):
                max_right = e["time"]
                break

        new_right_time = max(min_right, min(new_right_time, max_right))

        if next_event is not None and self._doc.is_blackout(next_event):
            if abs(next_event["time"] - new_right_time) < BLACKOUT_EPSILON:
                return False
            self._doc._set_event_time_internal(next_event["id"], new_right_time)
        else:
            current_right = (max_right if max_right != float('inf')
                             else self._doc._song_end_time())
            if new_right_time < current_right - BLACKOUT_EPSILON:
                self._doc._insert_blackout_internal(new_right_time, fixture)
            else:
                return False

        self._fades.clamp_group_to_visible(group_id)
        self._fades.regenerate_group(group_id)
        self._doc.reconcile_blackouts(fixture)
        self._commit()
        self.logger.info(
            f"Resized scene {group_id} right edge to {new_right_time:.3f}s"
        )
        return True
