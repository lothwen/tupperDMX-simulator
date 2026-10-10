"""
The timeline's selection and the clip clipboard.

The selection spans both halves of the scene — the lanes' clips and the song
sections on the waveform band — because one rubber band fills it and one group
drag moves it. Everything that has to hold for the pair (how far it may travel,
what a live preview slides, what a commit writes) lives here.

A mixin rather than a collaborator: the selection is rebuilt by the timeline's
own redraws (`_applying_selection` guards that from both sides) and a clip
delete elsewhere breaks the paste chain, so this state is the timeline's, not
something it could hand to another object.
"""

from typing import TYPE_CHECKING

from .tracks.midi_events import DraggableMidiEvent

if TYPE_CHECKING:
    from PyQt6.QtWidgets import QGraphicsLineItem, QGraphicsScene


class SelectionMixin:
    """Multi-selection that survives redraws, plus copy / delete / paste."""

    if TYPE_CHECKING:
        # Provided by Timeline, which creates them in its __init__.
        midi_manager: object
        content_scene: "QGraphicsScene"
        midi_events_tracks: list
        playhead: "QGraphicsLineItem"
        PIXELS_PER_SECOND: int
        _selected_event_ids: set
        _applying_selection: bool
        _paste_chain_from: object
        _paste_chain_next: float
        waveform_track: object

        def _schedule_refresh(self) -> None: ...
        def on_song_sections_deleted(self, event_ids) -> None: ...
        def on_song_sections_moved(self, moves) -> None: ...

    def selected_clip_items(self):
        """All currently-selected non-BLACKOUT clip items in the scene."""
        if self.content_scene is None:
            return []
        return [it for it in self.content_scene.selectedItems()
                if isinstance(it, DraggableMidiEvent) and not it.is_blackout_clip]

    def selected_section_boxes(self):
        """Selected song-section boxes — the waveform band's half of the
        selection, which a rubber band fills alongside the clips."""
        track = getattr(self, "waveform_track", None)
        if track is None:
            return []
        try:
            return track.sections.selected_boxes()
        except RuntimeError:
            # The scene is being torn down: it deletes its items and only then
            # clears its selection (see SongSectionOverlay._on_scene_selection).
            return []

    def selection_size(self) -> int:
        """How much is selected, both halves counted — what a delete removes,
        and what decides whether a drag is a group move or a plain click."""
        return len(self.selected_clip_items()) + len(self.selected_section_boxes())

    def _expanded_selected_event_ids(self):
        """Selected clips expanded to underlying event ids (scene members too)."""
        ids = set()
        for clip in self.selected_clip_items():
            if clip.group_event_ids:
                ids.update(clip.group_event_ids)
            else:
                ids.add(clip.event_id)
        return list(ids)

    def _on_selection_changed(self):
        """Keep the persisted id set + multi-select affordances in sync."""
        if self._applying_selection:
            return
        clips = self.selected_clip_items()
        self._selected_event_ids = {c.event_id for c in clips}
        multi = self.selection_size() >= 2
        for track in self.midi_events_tracks:
            for item in track.items:
                if isinstance(item, DraggableMidiEvent) and not item.is_blackout_clip:
                    item.set_multi_selected(multi and item.isSelected())

    def _reapply_selection(self):
        """Re-select clips by id after a redraw rebuilt every clip item."""
        if not self._selected_event_ids:
            return
        self._applying_selection = True
        try:
            restored = []
            for track in self.midi_events_tracks:
                for item in track.items:
                    if (isinstance(item, DraggableMidiEvent)
                            and not item.is_blackout_clip
                            and item.event_id in self._selected_event_ids):
                        item.setSelected(True)
                        restored.append(item)
            # Drop ids that no longer exist (merged / deleted clips).
            self._selected_event_ids = {it.event_id for it in restored}
            multi = len(restored) + len(self.selected_section_boxes()) >= 2
            for it in restored:
                it.set_multi_selected(multi)
        finally:
            self._applying_selection = False

    def selection_move_bounds(self):
        """(min, max) seconds the whole selection may travel.

        The clips' room and the sections' room INTERSECTED: one group move is
        one delta, so a distance either half cannot take is a distance neither
        takes. Both ranges contain 0 (where they sit now is legal), so the
        intersection always does too.
        """
        ranges = []
        ids = self._expanded_selected_event_ids()
        if self.midi_manager and ids:
            ranges.append(self.midi_manager.selection_move_bounds(ids))
        sections = (self.waveform_track.sections.selection_move_bounds()
                    if self.selected_section_boxes() else None)
        if sections is not None:
            ranges.append(sections)
        if not ranges:
            return (0.0, 0.0)
        return (max(lo for lo, _ in ranges), min(hi for _, hi in ranges))

    def selection_move_bounds_px(self):
        """(min_dx, max_dx) in scene pixels for moving the current selection."""
        lo, hi = self.selection_move_bounds()
        return (lo * self.PIXELS_PER_SECOND, hi * self.PIXELS_PER_SECOND)

    def capture_selected_clip_positions(self):
        """[(clip, pos, text_pos)] for the selected clips — where a live
        group-move preview slides them from. Captured once, at the press, by
        whichever grip started the drag."""
        peers = []
        for clip in self.selected_clip_items():
            text_pos = clip.text_item.pos() if clip.text_item is not None else None
            peers.append((clip, clip.pos(), text_pos))
        return peers

    def preview_selection_shift(self, clip_peers, delta_px: float):
        """Slide a group move in progress: the captured clips and the selected
        song sections, both by `delta_px`. Visual only — the grip's release
        commits, and `delta_px` 0 puts an abandoned preview back."""
        for clip, start_pos, text_start in clip_peers:
            clip.setPos(start_pos.x() + delta_px, start_pos.y())
            if clip.text_item is not None and text_start is not None:
                clip.text_item.setPos(text_start.x() + delta_px, text_start.y())
        self.waveform_track.sections.preview_shift(
            delta_px / self.PIXELS_PER_SECOND)

    def on_selection_moved(self, delta_sec: float):
        """Group-move drag committed — shift EVERYTHING selected by delta.

        Both grips end here, so it never matters whether the drag was taken by
        a clip or by a section box: the lanes and the arrangement strip travel
        the same distance, in one history batch, and come back on one Ctrl+Z.
        """
        if not self.midi_manager:
            return
        ids = self._expanded_selected_event_ids()
        moves = (self.waveform_track.sections.selection_moves(delta_sec)
                 if self.selected_section_boxes() else {})
        if not ids and not moves:
            return
        with self.midi_manager.history_batch():
            if ids and self.midi_manager.move_selection(ids, delta_sec):
                self._schedule_refresh()
            if moves:
                self.on_song_sections_moved(moves)

    def delete_selected(self):
        """Delete the whole selection outright — Ctrl+Z undoes it.

        One rubber band can catch clips and song sections together, so both
        halves go in one history batch and come back on one undo. They are
        different deletes: a clip leaves a BLACKOUT gap marker behind, a
        section marker just goes and the section before it extends.
        """
        if not self.midi_manager:
            return
        clips = self.selected_clip_items()
        sections = [b.event_id for b in self.selected_section_boxes()]
        if not clips and not sections:
            return
        ids = [c.event_id for c in clips]
        self._selected_event_ids = set()
        with self.midi_manager.history_batch():
            if ids and self.midi_manager.delete_selection(ids):
                self._paste_chain_from = None
                self._schedule_refresh()
            if sections:
                self.on_song_sections_deleted(sections)

    def copy_selected(self):
        """Copy the current clip selection into the clip clipboard."""
        if not self.midi_manager:
            return
        ids = self._expanded_selected_event_ids()
        if not ids:
            return
        self.midi_manager.copy_selection(ids)
        self._paste_chain_from = None

    def paste_at_playhead(self):
        """Paste clipboard clips at the playhead — or after the previous paste.

        Consecutive pastes with the playhead left alone chain: each block lands
        one block-length after the last, which is how a copied section is
        looped. Moving the playhead, copying again or deleting a clip breaks
        the chain, so the next paste starts over at the playhead. A block with
        no measurable length (every clip running to the song end) cannot chain,
        so it keeps pasting at the playhead.
        """
        if not self.midi_manager or not self.midi_manager.has_clipboard():
            return
        time_seconds = max(0.0, self.playhead.x() / self.PIXELS_PER_SECOND)
        chained = (self._paste_chain_from is not None
                   and abs(time_seconds - self._paste_chain_from) < 1e-4)
        target = self._paste_chain_next if chained else time_seconds
        if self.midi_manager.paste(target):
            span = self.midi_manager.clipboard_span()
            self._paste_chain_from = time_seconds
            self._paste_chain_next = target + span if span > 0 else target
            self._schedule_refresh()
