"""
Clip selection and the clip clipboard, for the timeline.

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

        def _schedule_refresh(self) -> None: ...

    def selected_clip_items(self):
        """All currently-selected non-BLACKOUT clip items in the scene."""
        if self.content_scene is None:
            return []
        return [it for it in self.content_scene.selectedItems()
                if isinstance(it, DraggableMidiEvent) and not it.is_blackout_clip]

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
        multi = len(clips) >= 2
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
            multi = len(restored) >= 2
            for it in restored:
                it.set_multi_selected(multi)
        finally:
            self._applying_selection = False

    def selection_move_bounds_px(self):
        """(min_dx, max_dx) in scene pixels for moving the current selection."""
        ids = self._expanded_selected_event_ids()
        if not self.midi_manager or not ids:
            return (0.0, 0.0)
        lo, hi = self.midi_manager.selection_move_bounds(ids)
        return (lo * self.PIXELS_PER_SECOND, hi * self.PIXELS_PER_SECOND)

    def on_selection_moved(self, delta_sec: float):
        """Group-move drag committed — shift the whole selection by delta."""
        if not self.midi_manager:
            return
        ids = self._expanded_selected_event_ids()
        if not ids:
            return
        if self.midi_manager.move_selection(ids, delta_sec):
            self._schedule_refresh()

    def delete_selected(self):
        """Delete every selected clip outright — Ctrl+Z undoes it."""
        if not self.midi_manager:
            return
        clips = self.selected_clip_items()
        if not clips:
            return
        ids = [c.event_id for c in clips]
        self._selected_event_ids = set()
        if self.midi_manager.delete_selection(ids):
            self._paste_chain_from = None
            self._schedule_refresh()

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
