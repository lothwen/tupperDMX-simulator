"""
What the timeline does when a clip is edited: drag commits, the clip editor,
deletes, song-section markers, and new clips from drops or the add dialog.

A mixin rather than a collaborator: the tracks, the content view and the
window all reach these as methods of the Timeline, and they share its refresh
scheduling, its cached events and its paste chain. Every write goes to the
MidiManager and ends in `_schedule_refresh()` — never a synchronous redraw,
because the caller is usually a scene item still on the call stack.
"""

from typing import TYPE_CHECKING, Dict, Iterable, Optional

from PyQt6.QtWidgets import QDialog

from core import note_layout
from core.clips import DEFAULT_DROP_VELOCITY
from .widgets.clip_edit import ClipEditDialog


class ClipCommandsMixin:
    """Edits to the song's clips, applied through the MidiManager."""

    if TYPE_CHECKING:
        # Provided by Timeline, which creates them in its __init__.
        midi_manager: object
        logger: object
        PIXELS_PER_SECOND: int
        _cached_events: list
        _paste_chain_from: object
        _transport_playing: bool
        library_changed: object      # pyqtSignal()
        preview_requested: object    # pyqtSignal(list)
        waveform_track: object

        def _schedule_refresh(self) -> None: ...
        def fixture_at_y(self, scene_y: float): ...
        def _fixture_type(self, channel): ...
        def _fdef(self, channel): ...
        def _dmx_start(self, channel) -> int: ...

    # --- Looking an event up by id -----------------------------------------------

    def _find_cached_event(self, event_id: str):
        """Look up a cached event by id."""
        for e in self._cached_events:
            if e.get('id') == event_id:
                return e
        return None

    def _group_id_for_event(self, event_id: str):
        """Return the scene group_id for an event_id, or None for singles."""
        evt = self._find_cached_event(event_id)
        return evt.get('group_id') if evt else None

    # --- Song sections: the arrangement overlay on the waveform band ------------
    # The overlay (tracks/song_sections.py) owns the gestures and the rules that
    # keep sections from reordering or stacking; these are its writes, each one
    # undo step. A song part is a global point-in-time marker, so none of the
    # fixture lanes' BLACKOUT gap machinery applies.

    def on_song_sections_moved(self, moves: Dict[str, float]):
        """Retime song-part markers, {event_id: new_time}, as one edit.

        Applied in the direction of travel: update_event_timing finds a
        marker's mirrored note by note number and OLD time, so a marker must
        never land on a time another of the same part still holds."""
        if not self.midi_manager or not moves:
            return
        old = {e['id']: e['time'] for e in self._cached_events
               if e.get('id') in moves}
        forward = any(moves[i] > old.get(i, moves[i]) for i in moves)
        order = sorted(moves, key=lambda i: old.get(i, 0.0), reverse=forward)
        with self.midi_manager.history_batch():
            for event_id in order:
                self.midi_manager.update_event_timing(event_id, moves[event_id])
        self._schedule_refresh()

    def on_song_sections_retyped(self, event_ids: Iterable[str], note: int):
        """Make each marker in `event_ids` a different part, keeping its time."""
        if not self.midi_manager:
            return
        ids = [i for i in event_ids
               if (self._find_cached_event(i) or {}).get('note') != note]
        if not ids:
            return
        with self.midi_manager.history_batch():
            for event_id in ids:
                self.midi_manager.edit_event(event_id, note=note)
        self._schedule_refresh()

    def on_song_sections_deleted(self, event_ids: Iterable[str]):
        """Delete markers outright, so the section before each one extends.
        Not confirmed — Ctrl+Z undoes it, as it does a clip delete."""
        if not self.midi_manager:
            return
        ids = list(event_ids)
        if not ids:
            return
        with self.midi_manager.history_batch():
            for event_id in ids:
                self.midi_manager.delete_event(event_id)
        self._schedule_refresh()

    def add_song_section(self, note: int, time_seconds: float,
                         velocity: int = DEFAULT_DROP_VELOCITY):
        """Start a `note` section at `time_seconds` (global: no channel)."""
        if not self.midi_manager:
            return
        if self.midi_manager.add_event(note, time_seconds, velocity, None):
            self._schedule_refresh()
            self.logger.info(
                f"Song section added: note={note} at {time_seconds:.3f}s")

    # --- Clip drags, committed on release ----------------------------------------

    def _commit_clip_drag(self, event_id: str, value: float, scene_op: str,
                          event_op: str, refresh: bool = True) -> None:
        """Apply a committed clip drag through the MidiManager method named
        `scene_op` when the event is part of a scene group — so all 7
        channel-control events change atomically — else `event_op`.

        Refreshes even when the manager rejects the commit, so the clip snaps
        back to its data-derived position instead of leaving a ghost preview
        at the dragged x. Fades pass refresh=False: their setters emit
        data_changed, which already drives the redraw."""
        if not self.midi_manager or not event_id:
            return
        group_id = self._group_id_for_event(event_id)
        if group_id:
            getattr(self.midi_manager, scene_op)(group_id, value)
        else:
            getattr(self.midi_manager, event_op)(event_id, value)
        if refresh:
            self._schedule_refresh()

    def on_midi_event_moved(self, event_id: str, new_time: float):
        """Whole-clip MOVE drag committed."""
        self._commit_clip_drag(event_id, new_time, "move_scene", "move_event")

    def on_midi_event_left_edge(self, event_id: str, new_time: float):
        """Left-edge resize drag committed."""
        self._commit_clip_drag(event_id, new_time, "set_scene_left_edge",
                               "set_event_left_edge")

    def on_midi_event_right_edge(self, event_id: str, new_right_time: float):
        """Right-edge resize drag committed."""
        self._commit_clip_drag(event_id, new_right_time, "set_scene_right_edge",
                               "set_event_right_edge")

    def on_midi_event_fade_in(self, event_id: str, seconds: float):
        """Fade-IN handle drag committed."""
        self._commit_clip_drag(event_id, seconds, "set_scene_fade_in",
                               "set_event_fade_in", refresh=False)

    def on_midi_event_fade_out(self, event_id: str, seconds: float):
        """Fade-OUT handle drag committed."""
        self._commit_clip_drag(event_id, seconds, "set_scene_fade_out",
                               "set_event_fade_out", refresh=False)

    # --- The clip editor ---------------------------------------------------------

    def on_midi_event_edit_by_id(self, event_id: str):
        """Edit dialog request for the event with `event_id`.

        Scene groups get the console; everything else is one clip on one
        fixture.
        """
        if not self.midi_manager or not event_id:
            return
        event_data = self._find_cached_event(event_id)
        if not event_data:
            return

        group_id = event_data.get('group_id')
        if group_id:
            self._edit_scene(group_id)
            return

        result = self._run_clip_dialog(ClipEditDialog.for_clip(
            **self._clip_dialog_fixture(event_data.get('channel')),
            note=int(event_data.get('note', 0)),
            velocity=int(event_data.get('velocity', 0)),
            time=float(event_data.get('time', 0.0))))
        if result is None:
            return
        if self.midi_manager.edit_event(event_id, note=result.note,
                                        velocity=result.velocity):
            self._retime_clip(event_id, event_data, result.time)
            self._schedule_refresh()
            self.logger.info(
                f"Clip edited: note={result.note}, velocity={result.velocity}, "
                f"time={result.time:.3f}s"
            )

    def _edit_scene(self, group_id: str):
        """Open the console for the scene group with `group_id`."""
        members = [e for e in self._cached_events
                   if e.get('group_id') == group_id]
        if not members:
            return
        channel = members[0].get('channel')
        # Channel value = velocity * 2 (the controller's convention). A channel
        # with no member is NOT part of this scene — it is shown at the
        # fixture's default and dimmed, and only joins if the user moves it.
        velocity_by_note = {m['note']: m['velocity'] for m in members}
        field_note = self.midi_manager.channel_note_map(channel)
        channels = {ch_name: min(255, velocity_by_note[note] * 2)
                    for ch_name, note in field_note if note in velocity_by_note}
        present = set(channels)
        current_name = (members[0].get('scene_name')
                        or f"Scene @ {members[0]['time']:.2f}s")

        result = self._run_clip_dialog(ClipEditDialog.for_scene(
            **self._clip_dialog_fixture(channel),
            name=current_name, channels=channels, present=present,
            time=float(members[0]['time'])))
        if result is None:
            return
        if self.midi_manager.update_scene(group_id, channels=result.channels,
                                          name=result.name,
                                          remove=result.removed):
            self._retime_scene(group_id, float(members[0]['time']), result.time)
            self._schedule_refresh()
            self.logger.info(
                f"Scene '{result.name}' updated (group_id={group_id})"
            )

    def _clip_dialog_fixture(self, channel) -> dict:
        """The ClipEditDialog arguments that describe the lane on `channel`:
        its fixture, the song's length and whether the lane is locked."""
        return dict(fdef=self._fdef(channel),
                    type_name=self._fixture_type(channel) or "",
                    channel=channel, dmx_start=self._dmx_start(channel),
                    song_duration=self.midi_manager.song_end_time(),
                    locked=self._is_locked(channel))

    def _run_clip_dialog(self, dialog):
        """Connect a clip dialog's outward signals, run it modally and return
        its values — None when it was cancelled or produced nothing."""
        dialog.library_changed.connect(self.library_changed)
        dialog.live_messages.connect(self._on_clip_edit_live)
        dialog.set_transport_playing(self._transport_playing)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return None
            return dialog.get_values()
        finally:
            dialog.close()

    def set_transport_playing(self, playing: bool) -> None:
        """Told by MainWindow. A clip dialog's live output is locked out while
        the sequencer runs — the worker's own note_ons would fight it."""
        self._transport_playing = bool(playing)

    def _on_clip_edit_live(self, messages):
        """Forward a clip dialog's live values, unless something says not to."""
        if self._transport_playing or not messages:
            return
        channel = messages[0][0]
        if self.midi_manager and (self.midi_manager.is_fixture_muted(channel)
                                  or self.midi_manager.is_fixture_locked(channel)):
            return
        self.preview_requested.emit(messages)

    def _retime_clip(self, event_id: str, event_data: dict,
                     new_time: float) -> None:
        """Move a clip to the time the dialog asked for, if it changed.

        Deliberately `move_event` rather than `edit_event(time=...)`: only the
        former searches for a free slot and keeps the BLACKOUT gap markers
        honest. It can refuse, and then the clip simply stays where it was.
        """
        if abs(new_time - float(event_data.get('time', 0.0))) < 1e-4:
            return
        if not self.midi_manager.move_event(event_id, new_time):
            self.logger.info(
                f"Clip kept at {event_data.get('time', 0.0):.3f}s — no room at "
                f"{new_time:.3f}s"
            )

    def _retime_scene(self, group_id: str, old_time: float,
                      new_time: float) -> None:
        """Same for a scene group."""
        if abs(new_time - old_time) < 1e-4:
            return
        if not self.midi_manager.move_scene(group_id, new_time):
            self.logger.info(
                f"Scene kept at {old_time:.3f}s — no room at {new_time:.3f}s"
            )

    # --- Delete ------------------------------------------------------------------

    def on_midi_event_delete_by_id(self, event_id: str):
        """Delete request for the event with `event_id`.

        BLACKOUT clips are removed outright (the user is clearing a gap).
        Regular clips are converted into a fixture-matching BLACKOUT instead
        of being deleted, so the time slot remains accounted for and adjacent
        clips don't visually expand into the freed space.

        Not confirmed — Ctrl+Z undoes it.
        """
        if not self.midi_manager or not event_id:
            return
        event_data = self._find_cached_event(event_id)
        if not event_data:
            return

        group_id = event_data.get('group_id')
        if group_id:
            ok = self.midi_manager.delete_scene(group_id)
        elif self.midi_manager.is_blackout(event_data):
            ok = self.midi_manager.delete_event(event_id)
        else:
            ok = self.midi_manager.replace_with_blackout(event_id)
        if ok:
            self._paste_chain_from = None
            self._schedule_refresh()

    # --- New clips: drops from the clip library, and the add dialog --------------

    def _is_locked(self, channel) -> bool:
        """Whether the lane is read-only. Drops already check this; editing
        did not, which was the one door left open."""
        if channel is None or not self.midi_manager:
            return False
        return bool(self.midi_manager.is_fixture_locked(channel))

    def can_accept_clip_drop(self, note: int, scene_y: float,
                             fixture_type: Optional[str] = None) -> bool:
        """Return True if a clip carrying `note` may land at `scene_y`.

        A note is droppable on a fixture lane when it is a meaningful verb for
        that fixture (channel offsets within range, scene/bank/chase verbs when
        the fixture defines them, global/utility verbs anywhere). A song part
        is global, so it lands on the arrangement from anywhere on the timeline
        — WHEN it lands is the section overlay's call (see preview_drop).

        When the payload names a `fixture_type` — the clip library tags its
        channel clips, whose meaning is the SOURCE fixture's channel list, not a
        slot number — the target lane's fixture must be that type, exactly as a
        scene drop is gated."""
        if not self.midi_manager:
            return False
        if note_layout.is_song_part(note):
            return True
        target_idx = self.fixture_at_y(scene_y)
        if target_idx is None:
            return False
        # A locked track rejects new clips.
        if self.midi_manager.is_fixture_locked(target_idx):
            return False
        if (fixture_type is not None
                and fixture_type != self._fixture_type(target_idx)):
            return False
        return note_layout.note_applies_to(note, self._fdef(target_idx))

    def handle_clip_drop(self, note: int, velocity: int, scene_x: float,
                         scene_y: float,
                         fixture_type: Optional[str] = None,
                         whole_bar: bool = False) -> bool:
        """Validate and insert a dropped clip. Returns True on success.

        A song part goes to the section overlay, which snaps it to a beat (a
        whole bar with `whole_bar`, i.e. Shift) and splits or retypes the
        section it lands in."""
        if note_layout.is_song_part(note):
            return self.waveform_track.sections.apply_drop(
                note, velocity, scene_x, whole_bar)
        if not self.can_accept_clip_drop(note, scene_y, fixture_type):
            return False
        time_seconds = max(0.0, scene_x / self.PIXELS_PER_SECOND)
        # Velocity 0 is floored to 1 so a plain verb's note_on is never mistaken
        # for a note_off — but on a value-bearing note the velocity IS the
        # value, and flooring it silently drops a chase clip onto the SECOND
        # chase (or a channel set onto 2 instead of 0).
        floor = 0 if note in note_layout.VALUE_BEARING_NOTES else 1
        clamped_velocity = max(floor, min(127, int(velocity)))
        # The note lands on the exact fixture instance it was dropped on.
        target_idx = self.fixture_at_y(scene_y)
        event_id = self.midi_manager.add_event(note, time_seconds,
                                               clamped_velocity, target_idx)
        if not event_id:
            return False
        self._schedule_refresh()
        self.logger.info(
            f"Clip dropped: note={note} at {time_seconds:.3f}s on "
            f"fixture #{target_idx}"
        )
        return True

    def can_accept_scene_drop(self, scene_y: float,
                              fixture_type: Optional[str] = None) -> bool:
        """A scene targets a track whose fixture defines scene banks, and whose
        type matches the scene's owning fixture (when the payload names one)."""
        if not self.midi_manager:
            return False
        target_idx = self.fixture_at_y(scene_y)
        # A locked track rejects new clips.
        if target_idx is not None and self.midi_manager.is_fixture_locked(target_idx):
            return False
        target_type = self._fixture_type(target_idx)
        fdef = self._fdef(target_idx)
        if not (fdef and fdef.banks):
            return False
        return fixture_type is None or fixture_type == target_type

    def handle_scene_drop(self, name: str, channels: dict,
                          scene_x: float, scene_y: float,
                          fixture_type: Optional[str] = None) -> bool:
        """Validate and insert a dropped scene. Returns True on success."""
        if not self.can_accept_scene_drop(scene_y, fixture_type):
            return False
        time_seconds = max(0.0, scene_x / self.PIXELS_PER_SECOND)
        target_idx = self.fixture_at_y(scene_y)
        group_id = self.midi_manager.add_scene(time_seconds, channels, name,
                                               target_idx)
        if not group_id:
            return False
        self._schedule_refresh()
        self.logger.info(
            f"Scene dropped: '{name}' at {time_seconds:.3f}s on fixture "
            f"#{target_idx} (group_id={group_id})"
        )
        return True

    def on_add_event_requested(self, time_seconds, fixture_index=None):
        """Add a new clip at `time_seconds`.

        `fixture_index` (the lane the request came from) targets that exact
        fixture; song parts ignore it and stay global.
        """
        if not self.midi_manager:
            return
        if self._is_locked(fixture_index):
            return

        result = self._run_clip_dialog(ClipEditDialog.for_clip(
            **self._clip_dialog_fixture(fixture_index),
            note=None, velocity=0, time=time_seconds, is_new=True))
        if result is None:
            return

        if result.kind == "new_scene":
            group_id = self.midi_manager.add_scene(
                result.time, result.channels, result.name, fixture_index)
            if group_id:
                self._schedule_refresh()
                self.logger.info(
                    f"Scene added: '{result.name}' at {result.time:.3f}s "
                    f"on fixture #{fixture_index} (group_id={group_id})"
                )
            return

        # Route to the lane's fixture unless it's a global song part.
        channel = (None if note_layout.is_song_part(result.note)
                   else fixture_index)
        event_id = self.midi_manager.add_event(result.note, result.time,
                                               result.velocity, channel)
        if event_id:
            self._schedule_refresh()
            info = note_layout.describe_note(result.note,
                                             self._fdef(fixture_index),
                                             result.velocity)
            name = info.part if info else f"Note {result.note}"
            self.logger.info(
                f"Clip added: {name} at {result.time:.3f}s, "
                f"velocity={result.velocity}"
            )
        else:
            self.logger.error(
                f"Failed to add clip: note {result.note} has no meaning here")
