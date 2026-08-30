"""FadeEngine — scene-group fade detection, generation, and UI setters.

A "fade" is a chain of note_on events on the fixture's OWN dimmer, ramping
linearly between (near) zero and the group's level V. Which note that is comes
from the fixture: a note is a verb ("set the n-th DMX channel"), so the ramp
rides `intensity_slot(fdef) + 1` — note 1 on a par, note 3 on a moving head
whose first two channels are Pan and Tilt — stamped with the group's MIDI
channel so it reaches that fixture and no other. A fixture with no master
dimmer (an RGBW wash, a hazer) has nothing for a ramp to ride and cannot fade.

A direct-channel clip fades the same way on its OWN note: there the velocity is
already the level (x2 = the DMX value), so the ramp keeps the note and moves the
velocity. Every other verb packs an index or a parameter into velocity — ramping
a chase clip would step through a hundred chases — so those never fade.

Fade notes live ONLY in midi_notes (never midi_events) so they don't pollute
scene clustering, timeline rendering, or per-clip mutation logic. They round-
trip through the saved MIDI file and are detected on load by the consecutive-
step-with-monotonic-velocity-and-zero-endpoint pattern.

Operates on a shared MidiDocument; the scene setters use injected commit /
notify-changed callbacks so the engine never touches Qt directly.
"""

from typing import Optional, List, Dict, Any, Tuple, Callable
import bisect
import uuid

from tupperlogger import get_logger
from core.fixture_library import intensity_slot
from core.note_layout import CHANNEL_OFFSET_RANGE
from .constants import (
    NOTE_MATCH_TOLERANCE,
    FADE_STEP_SECONDS,
    FADE_NOTE_DURATION,
    FADE_MIN_DURATION,
    FADE_OUT_END_GAP,
    FADE_MIN_RUN_NOTES,
    FADE_VELOCITY_MATCH_TOLERANCE,
)


class FadeEngine:
    def __init__(self, doc, commit: Callable[[], None],
                 notify_changed: Callable[[], None]):
        self._doc = doc
        self._commit = commit
        self._notify_changed = notify_changed
        self.logger = get_logger("midi.fade_engine")

    # --- what a fade rides ---------------------------------------------------

    def fade_note(self, channel) -> Optional[int]:
        """The note whose verb dims the fixture at `channel`, or None.

        Its Intensity slot + 1, resolved against the fixture's own def — never a
        fixed number, since channel 1 is the dimmer on a par and Pan on a moving
        head. None when the fixture has no master dimmer at all, and None when
        its dimmer sits past the last channel-offset verb: note 24 up LOADS A
        SCENE, the same tail `field_note_map` leaves unreachable.
        """
        fdef = self._doc._fdef_or_default(channel)
        if fdef is None:
            return None
        index = intensity_slot(fdef)
        if index < 0 or index + 1 > CHANNEL_OFFSET_RANGE[1]:
            return None
        return index + 1

    def _fade_target(self, group_id: str
                     ) -> Optional[Tuple[Dict[str, Any], int, Any]]:
        """``(dimmer member, its note, the group's MIDI channel)`` or None.

        None when the group is gone, its fixture has no dimmer, or the scene
        simply doesn't carry that channel — a scene submits only the faders that
        are present, so a dimmer-less scene has no level for a ramp to reach.
        """
        members = self._doc._group_members(group_id)
        if not members:
            return None
        channel = members[0].get('channel')
        note = self.fade_note(channel)
        if note is None:
            return None
        member = next((m for m in members if m['note'] == note), None)
        if member is None:
            return None
        return member, note, channel

    def can_fade_group(self, group_id: str) -> bool:
        """Whether this scene has a dimmer a fade ramp could ride."""
        return self._fade_target(group_id) is not None

    def _is_fadeable(self, event: Dict[str, Any]) -> bool:
        """Whether a ramp on `event` would mean anything.

        Only a channel-control clip qualifies: there the velocity IS the level,
        so ramping it dims that one channel. A grouped member belongs to its
        scene's ramp rather than one of its own, and a BLACKOUT is a gap marker
        with nothing to ramp.
        """
        doc = self._doc
        if event.get('group_id') or doc.is_blackout(event):
            return False
        return doc.is_channel_control_note(event['note'], event.get('channel'))

    def _fadeable_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        """The clip `event_id` names, when `_is_fadeable` says it can ramp."""
        idx = self._doc._index_of(event_id)
        if idx < 0:
            return None
        event = self._doc.midi_events[idx]
        return event if self._is_fadeable(event) else None

    def can_fade_event(self, event_id: str) -> bool:
        """Whether this clip's verb is one a velocity ramp would dim."""
        return self._fadeable_event(event_id) is not None

    @staticmethod
    def _owner_id(note: Dict[str, Any]) -> Optional[str]:
        """The scene group or clip whose ramp `note` is part of."""
        return note.get('parent_group_id') or note.get('parent_event_id')

    def _fade_owners(self) -> Dict[Tuple[bool, str], Any]:
        """``{(is_group, id): fixture}`` — everything that can carry a fade:
        one entry per scene group, one per ungrouped clip."""
        owners: Dict[Tuple[bool, str], Any] = {}
        for e in self._doc.midi_events:
            gid = e.get('group_id')
            key = (True, gid) if gid else (False, e['id'])
            owners.setdefault(key, self._doc._event_fixture(e))
        return owners

    # --- load-time detection -------------------------------------------------

    @staticmethod
    def _reverses(run_members: List[Dict[str, Any]],
                  candidate: Dict[str, Any]) -> bool:
        """Whether `candidate` turns a run that already has a direction around.

        With one member there is no direction yet, so nothing reverses it; the
        second member is what sets it.
        """
        if len(run_members) < 2:
            return False
        first, last = run_members[0]['velocity'], run_members[-1]['velocity']
        if last >= first:
            return candidate['velocity'] < last
        return candidate['velocity'] > last

    def detect_on_load(self) -> List[Dict[str, Any]]:
        """Spot fade ramps in freshly-parsed midi_events; migrate to midi_notes.

        Heuristic: any contiguous run of dimmer events at FADE_STEP_SECONDS
        apart (within NOTE_MATCH_TOLERANCE) with monotonic velocities and a
        zero endpoint is a fade. Detected events are removed from
        midi_events and the matching midi_notes entries are tagged
        is_fade=True. The parent_group_id is filled in later by
        attach_runs_to_groups once scene clustering has run.

        Candidates are split into LANES — one per (MIDI channel, note) — before
        any run is grown, because a run must never span two fixtures or two
        channels: two rigs dimming on the same 100 ms grid are two fades, not
        one. Every channel-control note is a lane, since a scene fades on the
        fixture's dimmer while a direct-channel clip fades on its own note.

        Returns the list of detected runs so the attach pass can find them.
        """
        doc = self._doc
        doc.midi_events.sort(key=lambda x: x['time'])

        lanes: Dict[Tuple[Any, int], List[Dict[str, Any]]] = {}
        # Memoized per lane: resolving a verb against its FixtureDef is cheap
        # once and expensive per event on a full song.
        is_control: Dict[Tuple[Any, int], bool] = {}
        for e in doc.midi_events:
            lane = (e.get('channel'), e['note'])
            if lane not in is_control:
                is_control[lane] = doc.is_channel_control_note(lane[1], lane[0])
            if is_control[lane]:
                lanes.setdefault(lane, []).append(e)

        runs: List[Dict[str, Any]] = []
        used_ids: set = set()

        for (lane_channel, lane_note), lane in lanes.items():
            # Secondary sort by velocity so at same-time conflicts the smaller
            # velocity comes first. This is what lets fade endpoints (vel=0)
            # win when they coincide with an original dimmer note (vel=V): for
            # fade-in, the original vel=V at scene_start sits next to the fade
            # starter vel=0; for fade-out, the fade ender vel=0 at clip_end
            # sits next to the next group's vel=V. Putting vel=0 first means
            # the run-extender picks the fade member, keeping the run
            # monotonic.
            candidates = sorted(lane, key=lambda e: (e['time'], e['velocity']))
            for i, anchor in enumerate(candidates):
                if anchor['id'] in used_ids:
                    continue
                run_members = [anchor]
                for j in range(i + 1, len(candidates)):
                    cand = candidates[j]
                    if cand['id'] in used_ids:
                        break
                    last_time = run_members[-1]['time']
                    expected = last_time + FADE_STEP_SECONDS
                    if cand['time'] > expected + NOTE_MATCH_TOLERANCE:
                        break  # gap too large — run ends here
                    if cand['time'] < last_time + NOTE_MATCH_TOLERANCE:
                        # Same-time / sub-tolerance duplicate (e.g. original
                        # group's dimmer note coincident with the fade
                        # starter). Skip without breaking the run.
                        continue
                    if self._reverses(run_members, cand):
                        # A fade-out ends FADE_OUT_END_GAP before the next
                        # clip, which puts the following clip's fade-IN within
                        # one step of it. Direction is what tells the two
                        # apart: a ramp only ever goes one way, so the first
                        # candidate that turns around ends this run and is
                        # left to anchor the next.
                        break
                    if cand['time'] < expected - NOTE_MATCH_TOLERANCE:
                        # Off-step endpoint: strictly between the last step
                        # and the next expected step. _linear_fade_ramp
                        # emits one of these whenever the user's chosen
                        # fade duration isn't a clean multiple of
                        # FADE_STEP_SECONDS (which is the common case —
                        # arbitrary drag positions rarely land on the grid).
                        # It is always the FINAL note in the ramp; absorb
                        # it and stop. Without this branch, fade-out's
                        # vels[-1]=1 endpoint is missed and the whole run
                        # fails the `vels[-1] <= 1` check.
                        run_members.append(cand)
                        break
                    run_members.append(cand)
                if len(run_members) < FADE_MIN_RUN_NOTES:
                    continue
                vels = [m['velocity'] for m in run_members]
                mono_up = all(vels[k] <= vels[k + 1]
                              for k in range(len(vels) - 1))
                mono_dn = all(vels[k] >= vels[k + 1]
                              for k in range(len(vels) - 1))
                # Endpoint floor is 1 (not 0) — see _linear_fade_ramp on why
                # velocity 0 can't be emitted into a MIDI file.
                is_fade_in = mono_up and vels[0] <= 1 and vels[-1] > vels[0]
                is_fade_out = mono_dn and vels[-1] <= 1 and vels[0] > vels[-1]
                if not (is_fade_in or is_fade_out):
                    continue
                kind = 'in' if is_fade_in else 'out'
                runs.append({
                    'kind': kind,
                    'channel': lane_channel,
                    'note': lane_note,
                    'members': run_members,
                    'start_time': run_members[0]['time'],
                    'end_time': run_members[-1]['time'],
                    'peak_velocity': max(vels),
                })
                for m in run_members:
                    used_ids.add(m['id'])

        if not runs:
            return []

        # Migrate: remove fade events from midi_events, tag corresponding
        # midi_notes entries. We match midi_notes by (channel, note, time,
        # velocity) since a fade-in starter at scene start collides with the
        # original dimmer note in (note, time) but differs in velocity.
        # Track which run each tagged midi_note belongs to via a temporary
        # `_fade_run_idx` field — attach_runs_to_groups consumes and clears it.
        #
        # midi_notes is indexed by lane and bisected rather than scanned: a
        # densely programmed song has thousands of both, and the naive
        # search is their product.
        index: Dict[Tuple[Any, int], Tuple[List[float], List[Dict[str, Any]]]] = {}
        for n in doc.midi_notes:
            times, entries = index.setdefault(
                (n.get('channel'), n['note']), ([], []))
            times.append(n['time'])
            entries.append(n)
        for times, entries in index.values():
            # parse_into appends a note when its note_OFF arrives, so the list
            # is in end-time order, not start-time order.
            ordered = sorted(zip(times, entries), key=lambda pair: pair[0])
            times[:] = [t for t, _n in ordered]
            entries[:] = [n for _t, n in ordered]

        for run_idx, run in enumerate(runs):
            for m in run['members']:
                times, entries = index.get(
                    (m.get('channel'), m['note']), ([], []))
                lo = bisect.bisect_left(times, m['time'] - NOTE_MATCH_TOLERANCE)
                hi = bisect.bisect_right(times, m['time'] + NOTE_MATCH_TOLERANCE)
                # First untagged entry of matching velocity wins.
                for n in entries[lo:hi]:
                    if n.get('is_fade'):
                        continue
                    if abs(n['velocity'] - m['velocity']) > FADE_VELOCITY_MATCH_TOLERANCE:
                        continue
                    n['is_fade'] = True
                    # The owner (a scene group or a clip) is filled in by the
                    # attach pass, which is what decides which of the two it is.
                    n['_fade_run_idx'] = run_idx
                    break

        detected_ids = used_ids
        doc.midi_events = [e for e in doc.midi_events
                           if e['id'] not in detected_ids]
        return runs

    def attach_runs_to_groups(self, fade_runs: List[Dict[str, Any]]) -> None:
        """Match each detected fade run to a scene group and fill in metadata.

        Fade-in matches an owner whose start time aligns with the run's first
        note. Fade-out matches an owner whose visible-clip-end aligns with the
        run's last note. An owner is a scene group — matched on the run's
        channel, and only when the run rides that fixture's dimmer note — or an
        ungrouped channel-control clip, matched on channel AND note, since a
        direct-channel clip fades on its own note. A ramp that reaches a
        different fixture or a different channel is not that owner's fade,
        however well the times line up. Unmatched runs are dropped — see the
        cleanup at the end.
        """
        if not fade_runs:
            return
        doc = self._doc

        # One candidate per thing that can own a fade, with the two anchors a
        # run is matched against and the note its ramp would ride.
        owners: List[Dict[str, Any]] = []
        seen_groups: set = set()
        for idx, e in enumerate(doc.midi_events):
            gid = e.get('group_id')
            fixture = doc._event_fixture(e)
            if gid:
                if gid in seen_groups:
                    continue
                seen_groups.add(gid)
                right = doc._group_right_edge(gid, fixture)
                note = self.fade_note(e.get('channel'))
            else:
                if not self._is_fadeable(e):
                    continue
                right = doc._right_edge_of(idx, fixture)
                note = e['note']
            if note is None:
                continue
            owners.append({
                'key': (True, gid) if gid else (False, e['id']),
                'channel': e.get('channel'),
                'note': note,
                'time': e['time'],
                'clip_end': (doc._song_end_time() if right == float('inf')
                             else right),
            })

        matched_run_idxs: set = set()
        for run_idx, run in enumerate(fade_runs):
            key = 'time' if run['kind'] == 'in' else 'clip_end'
            anchor = run['start_time'] if run['kind'] == 'in' \
                else run['end_time']
            target = next(
                (o for o in owners
                 if o['channel'] == run['channel'] and o['note'] == run['note']
                 and abs(o[key] - anchor) < NOTE_MATCH_TOLERANCE),
                None)
            if target is None:
                continue  # orphan run — its midi_notes are dropped below
            is_group, owner_id = target['key']
            duration = run['end_time'] - run['start_time']
            if run['kind'] == 'out':
                # The ramp deliberately stops short of the right edge, so the
                # duration it was generated from is that much longer.
                duration += FADE_OUT_END_GAP
            field = 'fade_in' if run['kind'] == 'in' else 'fade_out'
            targets = (doc._group_members(owner_id) if is_group
                       else [doc.midi_events[doc._index_of(owner_id)]])
            for m in targets:
                m[field] = duration
            parent_key = 'parent_group_id' if is_group else 'parent_event_id'
            for n in doc.midi_notes:
                if n.get('_fade_run_idx') == run_idx:
                    n[parent_key] = owner_id
            matched_run_idxs.add(run_idx)

        # Clean up: clear the temporary _fade_run_idx on all is_fade notes,
        # and DROP any orphan run that didn't match a group. Its events are
        # already out of midi_events, so keeping the notes would leave a ramp
        # that plays but appears nowhere and can never be regenerated — the
        # exact ghost a file written before fades resolved the fixture's own
        # dimmer carries (its ramps went to MIDI channel 0). The fade duration
        # itself isn't lost with them: it rides the tdmx1 scene meta.
        orphan_ids = {n['id'] for n in doc.midi_notes
                      if n.get('_fade_run_idx') is not None
                      and n['_fade_run_idx'] not in matched_run_idxs}
        for n in doc.midi_notes:
            n.pop('_fade_run_idx', None)
        if orphan_ids:
            doc.midi_notes = [n for n in doc.midi_notes
                              if n['id'] not in orphan_ids]
            self.logger.info(
                f"Dropped {len(orphan_ids)} orphan fade-ramp note(s) "
                f"belonging to no scene group")

    # --- ramp math -----------------------------------------------------------

    def _clamp_fade(self, requested: float, other_fade: float,
                    visible_duration: float) -> float:
        """Clamp a fade duration to [0, visible_duration - other_fade]."""
        room = max(0.0, visible_duration - max(0.0, other_fade))
        return max(0.0, min(requested, room))

    def _linear_fade_ramp(self, start_time: float, duration: float,
                          vel_start: int, vel_end: int,
                          step: float) -> List[Tuple[float, int]]:
        """Generate (time, velocity) tuples for a fade ramp.

        Notes are emitted at ``start_time, start_time+step, ...`` up to and
        INCLUDING ``start_time+duration``. The last point always lands at
        ``start_time+duration`` with velocity ``vel_end`` so endpoint values
        are exact even when ``duration`` isn't a clean multiple of ``step``.

        Velocities are clamped to a MINIMUM of 1: a MIDI ``note_on`` with
        velocity 0 is conventionally interpreted as ``note_off`` on parse,
        which would silently drop the fade endpoint on the next file load.
        Velocity 1 yields a DMX channel value of 2 — visually indis-
        tinguishable from 0 (~0.8% of full brightness) but round-trips
        through the MIDI file cleanly.
        """
        if duration < FADE_MIN_DURATION:
            return []
        notes: List[Tuple[float, int]] = []
        n_full = int(duration / step + 1e-9)
        for i in range(n_full + 1):
            t = start_time + i * step
            if t > start_time + duration + 1e-9:
                break
            frac = (t - start_time) / duration
            vel = round(vel_start + (vel_end - vel_start) * frac)
            notes.append((t, max(1, min(127, int(vel)))))
        end_time = start_time + duration
        if not notes or abs(notes[-1][0] - end_time) > 1e-6:
            notes.append((end_time, max(1, min(127, int(vel_end)))))
        else:
            notes[-1] = (end_time, max(1, min(127, int(vel_end))))
        return notes

    def _ramp(self, anchor: Dict[str, Any], right_edge: float
              ) -> List[Tuple[float, int]]:
        """(time, velocity) pairs for the fade-in and fade-out `anchor` asks
        for, ramping between (near) zero and its own velocity.

        The fade-out stops FADE_OUT_END_GAP short of the right edge: the codec
        sorts ramp notes after originals sharing a timestamp — which is what
        lets a fade-in's zero beat its own clip's value at the very same
        moment — so a fade-out landing exactly ON the boundary would overwrite
        the value the next clip just set.
        """
        if anchor["velocity"] <= 0:
            return []
        target_vel = int(anchor["velocity"])
        fade_in = max(0.0, anchor.get("fade_in", 0.0))
        fade_out = max(0.0, anchor.get("fade_out", 0.0))

        notes: List[Tuple[float, int]] = []
        if fade_in >= FADE_MIN_DURATION:
            notes.extend(self._linear_fade_ramp(
                anchor["time"], fade_in, 0, target_vel, FADE_STEP_SECONDS))
        if fade_out >= FADE_MIN_DURATION:
            notes.extend(self._linear_fade_ramp(
                right_edge - fade_out, fade_out - FADE_OUT_END_GAP,
                target_vel, 0, FADE_STEP_SECONDS))
        return notes

    def _generate_fade_notes(self, group_id: str) -> List[Tuple[float, int]]:
        """Compute (time, velocity) tuples for a group's fade-in + fade-out.

        Pure function over current state — caller is responsible for
        wiping prior is_fade entries and inserting these into midi_notes.
        Returns empty when the group has no dimmer to ramp (see
        `_fade_target`) or its dimmer is already at 0.
        """
        doc = self._doc
        target = self._fade_target(group_id)
        if target is None:
            return []
        dimmer, _note, _channel = target
        right_edge = doc._group_right_edge(group_id,
                                           doc._event_fixture(dimmer))
        if right_edge == float('inf'):
            right_edge = doc._song_end_time()
        return self._ramp(dimmer, right_edge)

    def _generate_event_fade_notes(self, event: Dict[str, Any]
                                   ) -> List[Tuple[float, int]]:
        """The same, for one direct-channel clip: its own note, its own
        velocity, and its visible right edge."""
        doc = self._doc
        idx = doc._index_of(event["id"])
        if idx < 0:
            return []
        right_edge = doc._right_edge_of(idx, doc._event_fixture(event))
        if right_edge == float('inf'):
            right_edge = doc._song_end_time()
        return self._ramp(event, right_edge)

    # --- generation / mutation ----------------------------------------------

    def _emit_ramp(self, is_group: bool, owner_id: str) -> None:
        """Append one owner's ramp notes to midi_notes. No wipe, no sort —
        the callers below own both, so a whole-lane rebuild pays for each
        exactly once instead of once per clip.

        Ramp notes are APPENDED so the stable sort by time keeps them after
        originals sharing a timestamp: that ordering is what lets a fade-in's
        zero beat its own clip's value at the moment the clip starts.
        """
        if is_group:
            target = self._fade_target(owner_id)
            if target is None:
                return
            _dimmer, note, channel = target
            ramp = self._generate_fade_notes(owner_id)
            parent = "parent_group_id"
        else:
            event = self._fadeable_event(owner_id)
            if event is None:
                return
            note, channel = event["note"], event.get("channel")
            ramp = self._generate_event_fade_notes(event)
            parent = "parent_event_id"
        for t, vel in ramp:
            self._doc.midi_notes.append({
                "id": str(uuid.uuid4()),
                "note": note,
                "time": t,
                "duration": FADE_NOTE_DURATION,
                "velocity": vel,
                # The channel selects the fixture: without it the codec
                # defaults the ramp to channel 0, where it would dim whichever
                # fixture is patched first instead of this one.
                "channel": channel,
                "is_fade": True,
                parent: owner_id,
            })

    def _regenerate_one(self, is_group: bool, owner_id: str) -> None:
        doc = self._doc
        parent = "parent_group_id" if is_group else "parent_event_id"
        doc.midi_notes = [
            n for n in doc.midi_notes
            if not (n.get("is_fade") and n.get(parent) == owner_id)
        ]
        self._emit_ramp(is_group, owner_id)
        doc.midi_notes.sort(key=lambda x: x["time"])

    def regenerate_group(self, group_id: str) -> None:
        """Wipe and rewrite this scene's ramp in midi_notes. Idempotent — call
        after any state change affecting the group's position, extent or
        level."""
        self._regenerate_one(True, group_id)

    def regenerate_event(self, event_id: str) -> None:
        """The same for one direct-channel clip: the ramp keeps the clip's OWN
        note and moves the velocity, since there the velocity is the level. A
        clip whose verb doesn't take a level gets nothing."""
        self._regenerate_one(False, event_id)

    def _rebuild(self, owners: Dict[Tuple[bool, str], Any], keys=None) -> None:
        """Re-derive `keys` (all owners by default) in ONE wipe and one sort.

        The wipe also takes any ramp left ownerless, which is what lets a
        delete need no wipe call of its own: the deleted clip is simply no
        longer an owner, so its ramp goes with it.
        """
        targets = list(owners if keys is None else keys)
        rebuilding = {key for _is_group, key in targets}
        live = {key for _is_group, key in owners}
        doc = self._doc
        doc.midi_notes = [
            n for n in doc.midi_notes
            if not (n.get("is_fade")
                    and (self._owner_id(n) in rebuilding
                         or self._owner_id(n) not in live))
        ]
        for is_group, key in targets:
            self._emit_ramp(is_group, key)
        doc.midi_notes.sort(key=lambda x: x["time"])

    def rebuild_all(self) -> None:
        """Re-derive every ramp in the document from its fade values.

        Ramp notes are derived state, so a load ends by regenerating them all:
        that is what re-emits a legacy file's ramps onto the fixture's own
        dimmer and MIDI channel once the sidecar has restored the durations.
        """
        self._rebuild(self._fade_owners())

    def regenerate_fixture(self, fixture) -> None:
        """Re-derive every ramp on one fixture's lane.

        Scoped to a fixture because that is exactly what one edit can disturb:
        the edited clip's own ramp, and the fade-out of whichever clip now ends
        where it starts — a fade-out is anchored on its clip's RIGHT edge,
        which is a property of its neighbour, not of itself.
        """
        owners = self._fade_owners()
        self._rebuild(owners,
                      [key for key, owner in owners.items() if owner == fixture])

    def clamp_group_to_visible(self, group_id: str) -> None:
        """Shrink fade_in/fade_out values that no longer fit a resized clip.

        Called after a left/right resize. Preserves the relative balance
        between in and out when both have to shrink. Idempotent.
        """
        doc = self._doc
        members = doc._group_members(group_id)
        if not members:
            return
        visible = doc._visible_duration_for_group(group_id)
        fade_in = max(0.0, members[0].get('fade_in', 0.0))
        fade_out = max(0.0, members[0].get('fade_out', 0.0))
        if fade_in + fade_out <= visible + 1e-9:
            return
        if fade_in + fade_out <= 0:
            return
        scale = visible / (fade_in + fade_out)
        new_in = max(0.0, fade_in * scale)
        new_out = max(0.0, fade_out * scale)
        for m in members:
            m['fade_in'] = new_in
            m['fade_out'] = new_out

    # --- UI setters ----------------------------------------------------------
    # Both pairs regenerate their ramp notes and COMMIT, so the value reaches
    # the file's tdmx1 sidecar meta as well as the ramp. Both refuse outright
    # when there is nothing to ramp: a scene with no dimmer, a clip whose verb
    # doesn't take a level.

    def set_event_fade_in(self, event_id: str, seconds: float) -> bool:
        evt = self._fadeable_event(event_id)
        if evt is None:
            return False
        doc = self._doc
        clamped = self._clamp_fade(seconds, evt.get('fade_out', 0.0),
                                   doc._visible_duration_for_event(event_id))
        if abs(clamped - evt.get('fade_in', 0.0)) < 1e-6:
            return False
        evt['fade_in'] = clamped
        self.regenerate_event(event_id)
        self._commit()
        return True

    def set_event_fade_out(self, event_id: str, seconds: float) -> bool:
        evt = self._fadeable_event(event_id)
        if evt is None:
            return False
        doc = self._doc
        clamped = self._clamp_fade(seconds, evt.get('fade_in', 0.0),
                                   doc._visible_duration_for_event(event_id))
        if abs(clamped - evt.get('fade_out', 0.0)) < 1e-6:
            return False
        evt['fade_out'] = clamped
        self.regenerate_event(event_id)
        self._commit()
        return True

    def set_scene_fade_in(self, group_id: str, seconds: float) -> bool:
        doc = self._doc
        members = doc._group_members(group_id)
        if not members or not self.can_fade_group(group_id):
            return False
        other = members[0].get('fade_out', 0.0)
        clamped = self._clamp_fade(seconds, other,
                                   doc._visible_duration_for_group(group_id))
        if abs(clamped - members[0].get('fade_in', 0.0)) < 1e-6:
            return False
        for m in members:
            m['fade_in'] = clamped
        self.regenerate_group(group_id)
        self._commit()
        return True

    def set_scene_fade_out(self, group_id: str, seconds: float) -> bool:
        doc = self._doc
        members = doc._group_members(group_id)
        if not members or not self.can_fade_group(group_id):
            return False
        other = members[0].get('fade_in', 0.0)
        clamped = self._clamp_fade(seconds, other,
                                   doc._visible_duration_for_group(group_id))
        if abs(clamped - members[0].get('fade_out', 0.0)) < 1e-6:
            return False
        for m in members:
            m['fade_out'] = clamped
        self.regenerate_group(group_id)
        self._commit()
        return True
