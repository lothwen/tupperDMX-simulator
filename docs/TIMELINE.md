# Timeline and clip editing

*The timeline is where a song's light show is authored. It has one lane per patched fixture, the song's audio as a
waveform band with the arrangement (song sections) on top of it, and a ruler carrying the song's markers. Every clip
is a MIDI note on its fixture's channel. The timeline draws the clips from `MidiManager`'s event list and writes every
edit back through `MidiManager`, which keeps the `.mid`, the undo history and the playing worker in step. Nothing on
screen is the data: every committed edit ends in a full redraw from the document.*

This document covers `timeline/` (including the clip editor in `timeline/widgets/clip_edit.py` and
`timeline/widgets/verb_editors.py`) and the editing half of `midi/`: `MidiManager`'s editing API, `MidiDocument`, the
clip, scene and selection editors, and the fade engine. It also covers `core/note_layout.py` and the drop payloads in
`core/clips.py` as far as editing reads them, and `MainWindow`'s wiring of the timeline. It does not cover the MIDI
playback worker and the transport (`docs/AUDIO.md`), the offset dialog the waveform drag opens (`docs/AUDIO.md` §7),
the tempo map and the marker track beyond what the ruler draws (CLAUDE.md, "The marker track"), the stage engine the
clip editor's preview runs on (`docs/STAGE.md`), or the auto program, which writes through `MidiManager.bulk_edit()`
(`docs/AUTOPROGRAM.md`).

Written 2026-10-04 against `betatest1-fixes` (commit `f33c001`).

---

## 1. What it does

One edit, from gesture to screen:

1. A gesture on a scene item (a clip drag, a fade handle, a section boundary, a drop, a dialog) previews **visually
   only**. Nothing is written while the mouse is down.
2. On release (or on the dialog's OK) the item emits a `*_committed` signal, or calls a `Timeline` method, carrying an
   event id or a scene `group_id` and the new value in **seconds**.
3. `Timeline` (`ClipCommandsMixin` / `SelectionMixin`) routes it: a scene group goes to the scene API, anything else to
   the single-event API. Edits with several parts run inside one `history_batch()`.
4. A `MidiManager` editor mutates `MidiDocument`: the UI list (`midi_events`) and its persistence mirror
   (`midi_notes`). It then reconciles the BLACKOUT gap markers on that fixture's lane, re-derives the lane's fade
   ramps, and commits.
5. `_commit()` rebuilds `midi_data` (the `mido.MidiFile`) from `midi_notes`, pushes it into a running worker in place,
   emits `data_changed` and records an undo snapshot.
6. `data_changed` reaches `ShowManager` (the debounced `.mid` autosave) and `MainWindow`, which calls
   `timeline._schedule_refresh()`. The edit's own handler has already scheduled one; they coalesce.
7. On the next event-loop tick `refresh_from_manager()` copies the events, re-spans the scene, rebuilds every lane's
   clips, restores the selection by id, syncs the header toggles and redraws the song sections.

```
scene item (clip / handle / box / drop / dialog)
  │ mouse move: preview only (setPos / setRect / _layout)
  │ release ≥ 1 px, or OK
  ▼
Timeline.on_*  ──────────────────────────────── ClipCommandsMixin / SelectionMixin
  │ group_id ? scene API : event API            history_batch() around multi-part edits
  ▼
MidiManager ─▶ ClipEditor / SceneEditor / SelectionEditor / FadeEngine
  │ MidiDocument: midi_events (UI) + midi_notes (mirror, what the .mid is built from)
  │ reconcile_blackouts(fixture) → regenerate_fixture(fixture) → _commit()
  ▼
_commit: codec.rebuild(midi_notes → midi_data) → worker.update_midi_data → data_changed → undo snapshot
  │
  ├─▶ ShowManager.on_midi_data_changed          ~3 s debounced .mid autosave
  └─▶ MainWindow → Timeline._schedule_refresh   QTimer.singleShot(0), coalesced
                      ▼
                  refresh_from_manager           deep copy, re-span, rebuild lanes, reselect, sections
```

---

## 2. Where the code lives

| File | Qt | What it does |
|---|---|---|
| `timeline/manager.py` | QtWidgets | `Timeline`: the two scenes and views, lane layout, reflow, redraws, zoom, the song's extent, the grid, the playhead. Mixes in the two below. |
| `timeline/clip_commands.py` | QtWidgets | `ClipCommandsMixin`: drag commits, the clip editor, deletes, song-section writes, drops and the add dialog. Every write goes to `MidiManager` and ends in `_schedule_refresh()`. |
| `timeline/selection.py` | QtWidgets | `SelectionMixin`: the one selection across clips and section boxes, group moves, delete, copy, paste and the paste chain. |
| `timeline/lane_reorder.py` | QtWidgets | `LaneReorder`: carrying a lane header to a new row by its grip. |
| `timeline/master_header.py` | QtWidgets | `MasterHeaderPanel`: the TRACKS counter and the MASTER / CLICK / VOICE volume rows. |
| `timeline/lanes.py` | none | Patch lookups: lane labels, lane order, channel → fixture instance → `FixtureDef`. |
| `timeline/arrangement.py` | none | Song-section rules on plain floats: spans, how far starts may move, what a drop does. |
| `timeline/tracks/base.py` | QtWidgets | `BaseTrack` and `elided_label()`. |
| `timeline/tracks/midi_events.py` | QtWidgets | `MidiEventsTrack` (one lane), `DraggableMidiEvent` (a clip), `FadeHandle`, `AutoSceneClip`, `TrackHeaderWidget`, `build_render_units()`. |
| `timeline/tracks/clip_geometry.py` | none | Drag maths: zones, bounds, snapping, interval clamping. |
| `timeline/tracks/song_sections.py` | QtWidgets | `SongSectionOverlay` and `SongSectionItem`: the arrangement strip on the waveform band and every gesture on it. |
| `timeline/tracks/waveform.py` | QtWidgets | `WaveformTrack` and `DraggableWaveform`: the band, its pixmap, the offset drag and click-to-seek. |
| `timeline/tracks/ruler.py` | QtWidgets | `RulerTrack` and `MarkerFlag`: bars or clock time, and the draggable marker flags. |
| `timeline/widgets/content_view.py` | QtWidgets | `ContentView`: rubber band, keyboard (Delete / copy / paste), Ctrl+wheel zoom, drag and drop from the clip library, empty-lane menus. |
| `timeline/widgets/clip_edit.py` | QtWidgets | `ClipEditDialog` and `ClipEditResult`: edit one clip or scene against its own fixture. |
| `timeline/widgets/verb_editors.py` | QtWidgets | One velocity editor per verb class, picked from a table. |
| `timeline/widgets/resize_handle.py` | QtWidgets | `TrackResizeHandle` (track heights) and `HeaderResizeHandle` (header strip width). |
| `timeline/widgets/tooltip.py` | QtWidgets | `CustomTooltip`, the one shared clip tooltip. |

Outside `timeline/`:

| File | What it does for the timeline |
|---|---|
| `midi/manager.py` | `MidiManager`: the editing API the timeline calls, undo/redo, `history_batch()`, `bulk_edit()`, the clip clipboard, the setup flags. |
| `midi/document.py` | `MidiDocument`: `midi_events`, `midi_notes`, lane scoping, BLACKOUT reconciliation, the atomic `_internal` mutators. Qt-free. |
| `midi/clip_editor.py` | `ClipEditor`: add, edit, delete, move and resize one clip; replace with BLACKOUT. |
| `midi/scene_editor.py` | `SceneEditor`: scene groups (add, move, update, delete, resize) and regrouping on load. |
| `midi/selection_editor.py` | `SelectionEditor`: rigid-block move and delete of a selection. |
| `midi/fade_engine.py` | `FadeEngine`: fade ramps as derived notes, their detection on load, the fade setters. |
| `midi/codec.py` | `MidiCodec`: `.mid` ↔ the two lists, and the `tdmx1:` text metas. |
| `midi/constants.py` | `MIN_EVENT_DURATION`, `BLACKOUT_EPSILON`, `NOTE_MATCH_TOLERANCE`, fade constants, `_channel_to_velocity`. |
| `core/note_layout.py` | The note protocol: verb ranges, `describe_note`, `note_applies_to`, `verb_choices`, `field_note_map`, setup and palette packing. Mirrors `handle_note_on`. |
| `core/clips.py` | `CLIP_MIME_TYPE`, `SCENE_MIME_TYPE`, `DEFAULT_DROP_VELOCITY` and the payloads the clip library drags. |
| `core/tempo_map.py` | The grid the ruler draws and clips snap to. |
| `core/markers.py` | Marker kinds, colours and glyphs for the ruler; `marker_at` for retiming. |
| `ui/main_window.py` | Builds the `Timeline`, wires its signals, owns Ctrl+Z / Ctrl+Shift+Z and Alt+←/→, persists heights, header width and lane order. |
| `ui/timeline_toolbar.py` | Zoom, Fit, Rows S/M/L, Bars/Time, beat division, Snap, Cues, Add track. |
| `ui/clip_library.py` | The drag source. |
| `ui/fixture/console.py`, `ui/fixture/affordances.py` | The faders, the preview card, the colour wheel and the capability picker the clip editor is built from. |

### Layering

- **The timeline never imports the `ui/` root, `app/`, `audio/` or `dmx/`.** What it needs from the window it asks
  for by signal: `fixture_edit_requested` (the fixture dialog lives in `ui/`), `preview_requested` (auditioning goes
  through `AppCore.preview_clip`, so `config.io` routing applies), `offset_edit_requested`, `seek_requested`. The
  waveform's samples arrive through an injected callable (`set_audio_source`), and the UI scale is passed in, because
  the audio engine and the theme are not the timeline's to import.
- **It does import from `ui.widgets`** (`icons`, `chips`, `sizing`), the leaf toolkit, and from
  `ui.fixture.console` / `ui.fixture.affordances` for the clip editor. It imports constants from `midi.constants`,
  `midi.scene_editor` and (lazily, in `MidiEventsTrack._compute_bounds`) `midi.manager`, and
  `software_controller.scene_loader` for the editor's library menu. Only `stage/` imports nothing from `ui/` at all.
- **No eager re-exports.** `timeline/__init__.py` re-exports nothing, because `Timeline`'s clip editor pulls in the
  stage engine (`ui.fixture.console` → `stage`). `timeline/widgets/__init__.py` re-exports the light widgets but
  **not** `clip_edit`, for the same reason: the tracks import that package. `timeline/tracks/__init__.py` does
  re-export its four tracks; none of them is heavy.
- **The rules live where they can be tested without a `QApplication`.** `lanes.py`, `arrangement.py` and
  `clip_geometry.py` are Qt-free, and so are `MidiDocument` and the three editors (`MidiManager` is the only
  `QObject` in `midi/`'s editing half, for its signals). The widgets are thin adapters that feed these zoom-adjusted
  numbers and apply the results.

---

## 3. The data model: what a clip is

### 3.1 Events and notes

`MidiDocument` holds two lists, both sorted by time:

- **`midi_events`**: one dict per `note_on` that means something on its lane. This is what the timeline draws
  (`get_events_for_ui()` hands out a deep copy).

  | Key | Meaning |
  |---|---|
  | `id` | `uuid4`, minted per load. Not stable across a save and reload. |
  | `note` | The verb (§3.2). |
  | `time` | Timeline seconds. |
  | `velocity` | The verb's parameter, 0–127. |
  | `channel` | MIDI channel 0–15: the fixture's stable `channel` in the patch, which selects the fixture. `None` for a song part authored global. |
  | `info` | `NoteInfo(part, color)` from `describe_note`, resolved against that channel's fixture. |
  | `group_id`, `scene_name` | Set on the members of a scene group (§3.5), else `None`. |
  | `fade_in`, `fade_out` | Seconds (§3.6). |

- **`midi_notes`**: the persistence mirror: `note_on`/`note_off` pairs with a `duration` (0.1 s for anything
  authored). It also carries what is never a clip: fade ramp notes (`is_fade`), the per-fixture palette (note 60) and
  the setup flags (note 71). **The `.mid` is rebuilt from `midi_notes`, never from `midi_events`** (`MidiCodec.rebuild`),
  and the worker plays that rebuilt file.

Every mutation keeps the two in step. A clip's mirror is found by note, **channel** and time (within
`NOTE_MATCH_TOLERANCE`, 5 ms, in `MidiDocument`'s `_internal` helpers; 10 ms in `ClipEditor`'s public ops), skipping
`is_fade` entries, which belong to their owner's ramp. The channel is part of the match because another lane can hold
the same note at the same time.

On load, the codec admits a `note_on` into `midi_events` only if it has a meaning on its channel's fixture
(`MidiDocument._note_info`, which asks `describe_note`). Velocity 0 is a `note_off` except on `VALUE_BEARING_NOTES` (1–35 and 70), where 0 is a real value.

### 3.2 The note is a verb, and a clip has no length

The note protocol is `software_controller/main.py`'s `handle_note_on`; `core/note_layout.py` is its reverse and must be
kept in step with it.

| Notes | Verb | Velocity means |
|---|---|---|
| 1–23 | Set the fixture's n-th DMX channel | value // 2 (the controller doubles it back) |
| 24–35 | Load scene (n − 24) of the current bank | blend time, tenths of a second |
| 36–47 | Select bank (n − 36) | nothing |
| 48 | Take manual control | nothing (64 by convention) |
| 49, 50, 51 | Manual, blackout this fixture, blackout all | nothing |
| 52–54 | Blend / colour-change / chase-change time | tenths of a second |
| 55–58 | Palette random on/off, scene prev/next | nothing (57/58: blend time) |
| 59 | Bank and scene packed | bank × 8 + scene |
| 60 | Palette: per-fixture **state**, not a clip (§3.7) | hue × 6 + mode |
| 61–69 | Song parts INTRO … END: global | nothing |
| 70 | Load chase | chase index |
| 71 | Setup flags: per-fixture **state**, not a clip (§3.7) | auto / mute / lock bits |
| 72–75 | Colour macros | nothing |

A note is an instant. **A clip starts at its time and runs until the next event on the same lane**; the last clip on a
lane runs to `song_end_time()`. Its drawn width is therefore derived, never stored, and two clips on one lane cannot
overlap: there is nowhere for an overlap to be written. "End this clip earlier" has to be said with another event,
which is what the BLACKOUT gap markers are (§3.4). `MIN_EVENT_DURATION` (10 ms) is the shortest a clip may get.

### 3.3 Which lane an event belongs to

`MidiDocument._event_fixture(event)` is the scoping key every lane computation compares:

- a **song part** (61–69) belongs to no lane and returns `None`, **asked of the note, not the channel**. A `.mid` can
  only carry a note on a real channel, so `rebuild` writes a global song part to channel 0 and the next load stamps 0
  back on it. Asked of the channel, every section would claim the lane of whatever is patched at channel 0 and act as
  an obstacle there;
- anything else returns its `channel`;
- channel-less legacy or synthetic events fall back to `info.fixture`, the type hint a test note map carries.

`MidiEventsTrack.matches_filter` gives the same answer, so the document and the timeline agree on who occupies a lane.
BLACKOUT ALL (51) is global at runtime but is dropped on a lane and stamped with its channel, so in the document it
belongs to that lane.

### 3.4 BLACKOUT gap markers

A BLACKOUT is any event whose `info.part` starts with `BLACKOUT` (case-insensitive): note 50, and also 51 BLACKOUT
ALL. It is identified by name prefix, not by number. The editors insert note 50 (`_blackout_note_number`, the first
`BLACKOUT*` entry in the note map, which `build_note_map` orders before 51) on the edited lane, wherever a clip has to
end before the next one starts.

- **They are never drawn.** `redraw_events` skips them, so they catch no hover, drag or menu. On screen a gap is just
  empty lane.
- **They are scoped per lane.** `reconcile_blackouts(fixture)` runs after every edit and enforces two invariants on
  that lane only: no BLACKOUT whose effective length (to the next event on the lane) is ≤ `BLACKOUT_EPSILON` (0.1 ms),
  and no two BLACKOUTs in a row (the later one goes). Both passes restart after each deletion.
- **Deleting a clip leaves its slot dark.** `replace_with_blackout` turns the clip's note into a BLACKOUT in place, so
  the clip before it does not grow into the freed space. Deleting a scene deletes its members and inserts one
  BLACKOUT at its time. A delete is not confirmed; Ctrl+Z undoes it.
- **A clip's trailing cap travels with it.** When a clip moves, the BLACKOUT right after it (its "trailing cap") is
  removed from the old place, and any BLACKOUT inside its new span is dropped.
- **Intended ends.** `move_event`, `move_scene` and `move_selection` snapshot every clip's intended right edge on the
  lane before the move, apply it, and then insert a BLACKOUT wherever a clip now runs past its intended end. That is
  how a move leaves its neighbours exactly as long as they were.

### 3.5 Scenes: groups of channel notes

A scene clip is several channel verbs (notes 1 … min(channel count, 23)) at one instant on one channel, plus a take-manual
verb (48, velocity 64) so the song-part program does not overwrite them next frame. All of them share a `group_id`
and a `scene_name`.

- `build_render_units()` collapses a group into **one** render unit, drawn as one clip. A group with a single member
  is drawn as a plain clip.
- On load the group ids are rebuilt: `SceneEditor.reattach_groups()` clusters two or more channel verbs on one
  channel within `BLACKOUT_EPSILON` of each other, folds a co-located note 48 into the group, and names it from the
  fixture's scene library when every channel matches (compared in velocity space), else `Scene @ 12.34s`. The
  `tdmx1:` text metas then pin the **stable** group id, the custom name and the fades (`_apply_sidecar_meta`); a group
  the meta cannot find within 20 ms (edited outside the app) stays with the heuristics.
- A channel value is stored as `value // 2`, floored to 1 (`_channel_to_velocity`): a `note_on` at velocity 0 would
  read as a `note_off`. So 255 replays as 254 and 1 as 2.
- **Only the channels a scene carries are in it.** At runtime an absent channel keeps whatever the previous frame left
  in it, while a present one at its default forces that value. `update_scene` therefore changes only the channels it
  is given, grows the group onto a channel only when asked, and removes a channel only through `remove=`, never below
  2 members (§12.2).

### 3.6 Fades

`fade_in` and `fade_out` are durations in seconds on the clip (on every member of a scene). The ramps themselves are
**derived notes** in `midi_notes`, tagged `is_fade` with a `parent_group_id` or `parent_event_id`:

- 100 ms steps (`FADE_STEP_SECONDS`), velocities linear between 1 and the clip's level, endpoints exact;
- a scene's ramp rides the fixture's **own dimmer**: `intensity_slot(fdef) + 1`, which must be ≤ 23. Note 1 on a par,
  note 3 on a moving head whose first channels are Pan and Tilt. A fixture with no master dimmer, or a scene that does
  not carry it, cannot fade (`can_fade_scene` is False and the handles are not built);
- a direct channel clip (1–23) fades on its own note, since its velocity is already the level. No other verb fades:
  its velocity is an index or a time (`can_fade_event`);
- the fade-out stops `FADE_OUT_END_GAP` (2 ms) short of the clip's right edge. Ramp notes sort after originals at the
  same timestamp, so a ramp landing exactly on the boundary would overwrite the value the next clip just set;
- **every edit re-derives the ramps of the whole lane** (`FadeEngine.regenerate_fixture`), because a fade-out is
  anchored on its clip's right edge, which is a property of the next clip;
- a resize shrinks fades that no longer fit, keeping their balance (`clamp_group_to_visible`, and the handles' preview).

The durations persist in the `tdmx1:` metas. A foreign file's ramps are recognised on load as runs of at least three
monotonic 100 ms steps ending near zero, and are rewritten onto the right dimmer and channel by `rebuild_all()`.

### 3.7 Per-fixture state: notes 60 and 71

Notes 60 (palette) and 71 (setup) sit at t = 0, at most one per channel, in `midi_notes` only. `describe_note` returns
`None` for both, so they persist and replay but never become a clip, no drop can author one, and `shift_all_events`
leaves them at t = 0. Absence is the default: all flags off has no note at all.

Note 71 packs the lane header's three toggles (`pack_setup`):

| Bit | Toggle | Effect |
|---|---|---|
| `0x01` | **A** (auto) | The fixture follows the song-part program. Mirrored onto the software controller; only enabled when one exists. The lane draws the read-only auto clips (§13). |
| `0x02` | **eye** (mute) | The worker skips the channel (pushed into a running worker on every commit). The lane is drawn at 35 % opacity. |
| `0x04` | **lock** | The lane's clips are not selectable, focusable or draggable and have no menu; drops are refused; the clip editor opens read-only. Playback is unaffected. |

Each toggle commits through `MidiManager.set_fixture_auto / _mute / _lock`, so it is an undo step and is written into
the `.mid`. The document is the source of truth for the auto flag: the controller's copy is seeded from it on a load,
when a controller attaches, and on every undo and redo (`seed_controller_auto`).

### 3.8 Song parts

Notes 61–69 are global verbs stored as **start points**. They are drawn on the waveform band as the arrangement strip
and never on a lane (§8).

---

## 4. Coordinates, zoom and the song's extent

- **Scene x is seconds × `PIXELS_PER_SECOND`** (20). It never changes. Zoom is the content view's horizontal scale
  only (`transform().m11()`): Ctrl+wheel steps it by 1.15 and floors zooming out at "fit"; the toolbar slider runs
  20–400 %; Fit sets it to viewport width / scene width. Every zoom path ends in `update_ruler_labels()`,
  `update_midi_text_truncation()` and `emit_zoom_changed()`.
- **Screen-pixel constants are divided by the zoom** wherever they become scene distances: the clip resize zone
  (`RESIZE_ZONE_PX` 8), the clip snap radius (`SNAP_PX` 6), a section edge (`EDGE_PX` 5), the retype radius
  (`RETYPE_PX` 8) and the marker snap (`MARKER_SNAP_PX` 6).
- **Text ignores the transform** (`ItemIgnoresTransformations`) and is elided to the clip's on-screen width
  (`elided_label`). A zoom re-fits every label from the width its clip was drawn at; section labels also re-fit on
  every horizontal scroll, since they pin to the visible part of their box.
- **Vertically**, the ruler is 40 px (its top 17 px are the marker lane), the waveform band is
  `waveform_track_height` (default 100), and the lanes stack flush below it. Every lane has the one shared
  `midi_events_track_height` (default 50), or `MIN_TRACK_HEIGHT` (30) when collapsed. A clip is inset 10 % of the lane
  height top and bottom.
- **The scene's width is the song's.** `apply_song_extent()` takes the longest of the backing, the guide and the MIDI
  length, plus 5 %; with nothing loaded it is 240 s. The audio length is also pushed into
  `MidiDocument.set_song_duration()`, so clip-edit bounds cover the whole song. `song_end_time()` is the maximum of
  the MIDI length, that audio length and 240 s. Lanes draw their last clip to `song_end_time() × 1.05`; the last song
  section ends at `song_length()`, the true length.
- **The header strip** is `HEADER_WIDTH` 240 × UI scale, or the width the divider was dragged to (clamped to
  170 × scale … 600). It is painted, not styled, so `set_ui_scale()` re-measures it, unless the user has dragged it,
  in which case it is only re-clamped. The header view has no scrollbar and mirrors the content view's vertical scroll;
  `_sync_header_scroll_range()` tops its scene up so the two scroll ranges stay equal.

Stacking order in the content scene:

| z | Item |
|---|---|
| −3, −2 | Lane tints (`LANE_TINT_STRENGTH` 0.16 of the type colour) and lane separators |
| −1 | Grid lines |
| 0 | Ruler background and ticks |
| 0.5, 0.6 | Waveform pixmap; auto clips and their labels |
| 1, 2 | Clips and their labels; section boxes and their header text |
| 3, 4 | Fade handles (children of their clip); the drop preview; marker flags (4) |
| 10 | Playhead |

---

## 5. Redraws and item lifetime

The two scenes are rebuilt differently. The **header scene** holds persistent `QGraphicsProxyWidget`s: the master
header and one `TrackHeaderWidget` per lane, built once per patch and only moved and resized on a reflow. The
**content scene** is rebuilt: every clip, label, section box and marker flag is a new item after each refresh.

- **Never refresh synchronously from a scene item's handler.** A commit fires from inside a clip's
  `mouseReleaseEvent`, a menu action or a drop, and a synchronous redraw destroys the item while Qt is still unwinding
  its event: a use-after-free crash. Use `_schedule_refresh()`, a zero-timer that coalesces with the one
  `data_changed` schedules. `refresh_from_manager()` is for callers outside the scene: a song load, the playlist.
- **Retired items die in stages.** `redraw_events`, the section overlay and the ruler's flags all hide, disable,
  `prepareGeometryChange()`, remove and `deleteLater()` an item, and keep the Python reference alive for one more
  cycle (`_stale_items`, `_stale`, `_stale_markers`). The content scene also uses `NoIndex` instead of the BSP tree,
  which kept stale pointers across rapid remove/re-add; a linear scan is fine for a few hundred items.
- **The selection survives a redraw by id.** `_selected_event_ids` is restored by `_reapply_selection()`, and
  `_applying_selection` stops the teardown's `selectionChanged` from wiping it. The section overlay keeps its own id
  set the same way.
- **One tooltip.** `Timeline.clip_tooltip()` is a single `CustomTooltip`, a child widget rather than a tooltip window
  (mapping a native tooltip window was measured at 111 ms on the Qt thread). `redraw_events` hides it first, so a clip
  destroyed under the cursor cannot leave it on screen.
- **Read what you need before `exec()`.** A menu or modal dialog runs an event loop, which can run a scheduled redraw
  and free the item that opened it. The section menu reads everything off its box first; the lane header's menu is
  unparented (a popup parented to a proxied widget is embedded in the scene and clipped by it) and placed at
  `QCursor.pos()`.
- **A reflow is cheaper than a refresh.** `_reflow_layout()` (a height, width or patch change) re-lays headers, redraws
  lane content from the **cached** events, and rebuilds the waveform pixmap only on a commit, not on every preview
  step. A lane reorder does not even redraw: it moves the existing items (§9.2).
- `rebuild_for_patch()` is safe to call synchronously: its trigger is a designer button, not a timeline item.

---

## 6. Clips on a lane

### 6.1 Zones and modes

`clip_geometry.zone_at()` classifies a press: the outer `RESIZE_ZONE_PX` on each side is RESIZE_LEFT / RESIZE_RIGHT
and the rest is MOVE. A clip narrower than two zones splits half and half between the two resizes. A clip in a
multi-selection (§7) is all MOVE, and its drag moves the whole selection. The cursor follows the zone on hover.

### 6.2 Bounds

`MidiEventsTrack._compute_bounds()` projects the lane's render units onto `Occupant(time, is_blackout)` and
`compute_bounds()` returns a `ClipBounds` in scene pixels:

- **`move_intervals`**: every free slot on the lane wide enough for the clip, where BLACKOUTs are not obstacles. There
  can be several, so a clip can jump over its neighbours into a gap far enough away. The last clip on a lane is
  treated as point-sized, so it can move freely.
- **Resize left** may run back to the previous non-BLACKOUT clip (+ `MIN_EVENT_DURATION`), consuming the BLACKOUT
  between; **resize right** may run up to the next non-BLACKOUT clip.
- **`snap_xs`**: every other unit's start and end, t = 0 and the song end.

### 6.3 Snapping

During a drag the proposed edge snaps to whichever is nearest within `SNAP_PX / zoom`: a neighbour edge, or the grid
line `Timeline.snap_grid_x()` returns. In Time mode that is the nearest second; in Bars mode it is the tempo map's
nearest line at the toolbar's beat division (1 = bars, 4 = beats, 8 = half-beats). It is a callable rather than a
spacing because across a tempo marker the lines on either side are different distances apart. Snap off disables both
kinds. A move re-clamps into its slot after snapping; a resize does not, so a snap can pull an edge slightly past its
bound, and the manager clamps it on commit.

### 6.4 Committing

On release, a drag of at least 1 px emits `move_committed`, `left_edge_committed` or `right_edge_committed` with a time
in seconds. **A pure click commits nothing**: a commit refreshes, and the refresh would destroy the item and the
selection the press just made. `Timeline._commit_clip_drag()` sends a scene group to `move_scene`,
`set_scene_left_edge` or `set_scene_right_edge` and anything else to the `*_event` equivalent, then schedules a
refresh **even if the manager refused**, so the clip snaps back to where the data says it is.

What the manager does:

| Op | Rule |
|---|---|
| `move_event` / `move_scene` | Keeps the clip's width. Picks the free slot whose clamped position is nearest the request; refuses if none fits. Carries the trailing cap, drops BLACKOUTs inside the new span, re-caps neighbours from their intended ends. |
| `set_event_left_edge` / `set_scene_left_edge` | Moves the start only. Growing left consumes a BLACKOUT before it; shrinking inserts a BLACKOUT at the old start if the previous event was a clip. |
| `set_event_right_edge` / `set_scene_right_edge` | Moves the following BLACKOUT, or inserts one to shrink. Cannot grow past the next clip. |
| `move_selection` | §7. |

### 6.5 Fade handles

A fadeable clip that is not locked carries two `FadeHandle` triangles on its top edge. They are clamped inside the
clip and cannot cross each other (no cross-fades); the clip previews the diagonal while one is dragged. The release
commits through `set_event_fade_in/out` or `set_scene_fade_in/out` with `refresh=False`, because those setters commit
and their `data_changed` drives the redraw.

### 6.6 Everything else on a lane

| Gesture | Result |
|---|---|
| Double-click a clip | Clip editor (§12). |
| Right-click a clip | Edit Properties… / Add event at this position / Delete Event. With 2+ selected: one "Delete N events". |
| Right-click empty lane | Add event at this position (the add dialog, §11.3). |
| Ctrl+click a clip | Toggle it in the selection, no drag. The matching release is swallowed, or Qt toggles it back. |
| Press empty lane, drag ≥ 4 px | Rubber band (§7). Ctrl adds to the selection. |
| Click empty lane | Seek. A bare Ctrl+click there does nothing. |
| Hover a clip | The shared tooltip: part, note, time, bar and beat, velocity. |

A locked lane's clips swallow the press and offer no menu; the tooltip still works.

---

## 7. Selection, group moves and the clipboard

**There is one selection across both halves of the scene**: the lanes' clips and the song-section boxes. A rubber band
started on empty lane space and dragged up into the waveform band catches boxes as well as clips
(`ContentView._update_rubber_selection` → `SongSectionOverlay.select_in_rect`). `Timeline.selection_size()` counts
both halves, and it is what decides whether a drag is a group move and what "N events" in the menus counts.

- **Two or more selected** puts every selected clip in multi mode: fade handles hidden, no resizing, the whole body a
  group grip. A lone section box never moves (§8.3); one selected with something else is a group grip too.
- **A group move is one delta from either grip.** At the press the grip captures where the selected clips sit
  (`capture_selected_clip_positions()`) and the allowed range (`selection_move_bounds()`): the **intersection** of
  `MidiManager.selection_move_bounds()` for the clips and `arrangement.move_bounds()` for the sections. Both contain 0,
  so the intersection does. The preview slides both halves (`preview_selection_shift()`); a box grip snaps its own
  start to the beat grid, a clip grip does not snap. The release calls `on_selection_moved(delta)`, which runs
  `move_selection` and the section moves in **one** `history_batch`, so one Ctrl+Z.
- **`move_selection`** moves the clips as a rigid block. Clips outside the selection on the same lanes are the
  obstacles; selected clips and BLACKOUTs are not. It then drops every BLACKOUT on the affected lanes and re-caps each
  render unit (a scene counts as one) from its intended edge.
- **Delete** (the Delete key in the content view, or "Delete N events") removes both halves in one batch: clips become
  BLACKOUTs as in §3.4, section markers simply go and the section before each extends. Not confirmed.
- **Alt+←/→** is handled by `MainWindow.eventFilter`. It calls `sections.nudge_selected()`: the selection moves one
  bar (Shift: one beat) along the tempo map, all of it or none of it, with any selected clips riding along. It only
  acts when at least one section is selected; otherwise Alt+arrows seek like bare arrows.
- **Copy and paste are for clips only** (a section is a point in the arrangement, not something to repeat). Ctrl+C
  and Ctrl+V are handled by `ContentView.keyPressEvent`, so only while the timeline has focus.
  - `copy_selection()` stores each render unit relative to the earliest one: its width (`None` if it runs to the song
    end), its fades, and either the scene's channel values or the note and velocity, plus its channel. BLACKOUTs are
    skipped.
  - `paste(at)` recreates the units left to right in one batch: `add_scene` or `add_event`, then the right edge, then
    the fades.
  - **The paste chain**: pasting again with the playhead untouched lands the next block one `clipboard_span()` after
    the previous one, so Ctrl+V repeated loops a copied section. Moving the playhead, copying again or deleting a clip
    breaks the chain. A block whose every clip runs to the song end has no span and pastes at the playhead.

### 7.1 Undo and redo

Ctrl+Z and Ctrl+Shift+Z are window-level `QShortcut`s calling `MidiManager.undo()` / `redo()`.

- **Every `_commit()` records a snapshot**: deep copies of `midi_data`, `midi_events` and `midi_notes`. The history
  holds 50 and is reset on `load_from_file` and `create_empty_track`, so a song switch starts it fresh.
- **Restoring a snapshot** (`_apply_history_state`) also re-seeds the live controller's auto flags from the restored
  note-71 notes, pushes the data and the muted channels into a running worker, and emits `data_changed`. It records
  nothing itself.
- **`history_batch()`** turns everything inside it into one step, and pushes only if something committed. Nested
  batches join the outer one.
- **`bulk_edit()`** is a batch that also defers the rebuild, the worker push, `data_changed` and the fade ramps to the
  end of the block, and quietens the editors' per-edit log lines. The auto program uses it to write thousands of clips.
- **Not in the history**: lane order, track heights, the header width, collapse state and ruler marker drags. They are
  show or machine settings, not the `.mid`.

---

## 8. Song sections: the arrangement strip

### 8.1 The model

A section is one song-part note: where that section starts. `arrangement.spans()` turns the sorted starts into
contiguous boxes, each running to the next start and the last to the song's true end. Every edit is therefore an edit
of a boundary, and two rules keep the strip honest (`timeline/arrangement.py`, on plain floats):

- **Nothing reorders or stacks.** `move_bounds(starts, moving, song_end, min_gap)` is how far a set of starts may shift
  together without crossing or crowding an unmoved neighbour, staying inside [0, song end − gap]. The gap is one beat
  (the shortest bar ÷ beats per bar). An arrangement already tighter than that may still move away from the crowding.
- **A drop splits or retypes.** `resolve_drop(starts, t, radius)` retypes the section starting within `radius` of `t`
  (`max(one beat, RETYPE_PX / zoom)`), and otherwise inserts a new start, splitting the section it lands in. A drop in
  the last beat before the song end is refused.

### 8.2 Drawing

Each section is a `SongSectionItem` spanning the whole band, with a header bar (`HEADER_PX` 18 × UI scale) in the
part's colour carrying its name and its length in bars, rounded to the beat. The waveform is drawn below the header bar,
so no bar covers a peak. Labels pin to the visible left of their box and drop the length first, then elide, as room
runs out. The waveform is recoloured per section through its alpha (`DraggableWaveform.set_tints`); see §18 for what
that currently shows.

### 8.3 Who gets the press

**The bar is the section; the waveform below it is the audio.** A box's tooltip, menu and boundary grips reach down
the whole band, but a plain left press below the header bar is **ignored**, so the scene offers it to the waveform
item underneath: a drag there slides the audio offset (`docs/AUDIO.md` §7), and a click of less than
`CLICK_SLOP_PX` (4) seeks. Without that, a song sectioned end to end would have no waveform left to grab.

| Gesture | Result |
|---|---|
| Drag an edge (`EDGE_PX` either side, full band height) | Moves that boundary: snapped to the beat (Shift: the bar; Snap off: none), clamped by `move_bounds`. The last box's right edge is the song end, not a boundary. |
| Click a header bar | Selects it alone and seeks there. Ctrl toggles; Shift selects the range from the anchor. |
| Drag across header bars from a click | Sweeps a selection (Ctrl: adds to it). |
| Drag a selected bar, 2+ selected (timeline-wide) | Group move (§7). |
| Drag a lone bar | Nothing. The audio under it does not move, so neither does it. |
| Double-click a bar | A menu of parts: retype it. |
| Right-click a box | Change to ▸ · Split here ▸ · Play from 'X' · Select all sections · Delete 'X' (the previous one extends) · Set audio offset… |
| Right-click the band outside a box | Add section here ▸, or Change 'X' to ▸ on a start · Set audio offset… |
| Delete / Backspace on a focused box | Delete the selected sections. |

All of it is owned by `SongSectionOverlay`, because one boundary or group move reshapes several boxes; the boxes only
forward their events. A gesture previews by re-laying the strip from a proposed list of starts (`_layout`) and
commits once, on release.

### 8.4 The writes

| Timeline method | Manager call | Note |
|---|---|---|
| `on_song_sections_moved({id: t})` | `update_event_timing`, one batch | Applied in the direction of travel: the call finds the mirrored note by number and **old** time, so a marker must never land on a time another of the same part still holds. |
| `on_song_sections_retyped(ids, note)` | `edit_event(note=)`, one batch | Keeps the time. |
| `on_song_sections_deleted(ids)` | `delete_event`, one batch | |
| `add_song_section(note, t)` | `add_event(..., channel=None)` | Global. |

None of the BLACKOUT machinery applies: a section is no lane's occupant.

---

## 9. Lane headers and the track strip

### 9.1 The header

`TrackHeaderWidget`, left to right: the reorder grip, the collapse chevron, a swatch in the fixture's type colour
(`color_for_type`, the same colour as its clip-library section), the name (`lanes.fixture_labels`: the instance name,
else the type, numbered when it repeats) over `Ch. n · DMX a–b`, and the eye, lock and **A** toggles (§3.7).

- The channel is shown 0-based, as everywhere else in the app.
- Hovering the header replaces the address line with "Double-click to edit". A double-click, or the right-click
  menu's one action, emits `fixture_edit_requested(channel)`; `MainWindow.open_fixture_editor` opens the dialog on a
  zero timer, because the dialog can rebuild the very header that asked.
- A DMX clash anywhere in the patch turns the address row red and bold, with the clashing fixtures in its tooltip
  (`set_dmx_clash` with `dmx_clashes`). It is re-asked on every reflow, so it clears as soon as the patch is fixed.
- The header accepts a plain left press on purpose: an ignored press inside a `QGraphicsProxyWidget` means no
  double-click is ever delivered.

### 9.2 Lane order and reordering

Lanes are **displayed** in the row they were dragged to and **bound** by the fixture's stable channel, so reordering,
adding or removing a fixture never re-binds a clip. `lanes.display_key` sorts placed fixtures (`order` ≥ 0) by order
and unplaced ones (`order` −1, i.e. new, or from a show saved before reordering) after them by DMX address, so an
untouched patch lays out as it always did.

`LaneReorder` carries a lane:

- The grip's press starts it. The carry rides the **header viewport's** own mouse moves through an application-wide
  event filter, in viewport coordinates. A proxied widget's global positions are not trustworthy under Wayland: a
  client is never told where its window is, and when that origin shifted mid-drag the lane jumped the height of the
  whole strip.
- It restacks only when the target row changes, by **moving** each lane's existing items (`move_content_to`), not
  redrawing them. A full redraw per crossing cost ~65 ms on a 240-clip song, enough to swallow mouse events.
- The target row is the nearest slot among the tops the lanes would have with the carried one taken out, so a lane
  does not swap the moment it is grabbed and the swap point is the same in both directions.
- It ends on any release in the app, on any mouse move with no button held, or from a 250 ms watchdog (no button held
  and 0.4 s without movement). Anything that should never happen (re-entry, the strip scrolling, a lost release) is
  logged once per kind per drag.
- The release runs one full `_reflow_layout()` and, if the row changed, emits `track_order_changed([channels])`.
  `MainWindow.on_track_order_changed` stamps `order` on **every** patch entry and saves the show.

### 9.3 Heights, collapse and width

- **Track heights**: one grip at the bottom of the waveform band, and one shared grip at the bottom of the last lane
  that resizes every lane. A drag previews with a reflow; the release rebuilds the waveform pixmap and emits
  `track_heights_changed`, persisted in `config.window` (userprefs).
- **Collapse** is view-only: a lane's chevron shrinks it to 30 px and hides the address line. The toolbar's Rows preset
  S collapses every lane (the window remembers that in `config.window.tracks_collapsed`); M and L expand them and set
  the shared height to 50 or 80.
- **Header width**: the divider between the strip and the content (`HeaderResizeHandle`) emits
  `header_width_changed`, persisted as `config.window.timeline_header_width`.

### 9.4 The master header

`MasterHeaderPanel` sits over the ruler and the waveform band's header: the TRACKS counter and the MASTER, CLICK and
VOICE rows (mute, 0–100 slider, dB read-out). It reports through its own six signals, which the `Timeline` re-emits.
`set_volumes()` moves the sliders with their signals blocked, for a live userprefs reload.

---

## 10. The ruler

- **Bars mode** walks `tempo_map.grid_lines(until, beat_division)`: numbered bar lines and shorter subdivision ticks,
  at each bar's real width after a tempo marker. When the **narrowest** bar is under 40 px only bar 1 and every 5th
  keep their number. **Time mode** draws a tick per second and an `m:ss` label every 5 s, thinned the same way.
- **The lanes' grid lines** (`draw_time_grid`) come from the same walk, so a clip snaps to a line that is drawn.
- **Tempo** arrives as a whole map (`set_tempo_map`, from `AppCore.tempo_map_changed`). The scene's x axis stays
  seconds, so a tempo change moves the grid under the clips and never the clips.
- **Markers.** Voice cues and control markers (tempo, stop click, segue) are drawn as `MarkerFlag`s in the top 17 px:
  a stem, a flag with the kind's glyph, and a name chip that hides when the next flag is too close. They are painted in
  device pixels. Pointing at or dragging one drops a dashed line through the whole arrangement.
  - A drag snaps to the grid within 6 px and commits on release as `marker_move_committed(kind, old_time, new_time)`.
    The flags are drawn from tuples, so the old time is how `MainWindow.on_marker_moved` finds the marker
    (`core.markers.marker_at`); it retimes the song's config and re-applies the marker track. This is not a `.mid`
    edit and is not undoable.
  - A double-click emits `marker_edit_requested(kind, time)`, which opens the Cues dialog. Text, BPM, adding and
    removing are that dialog's job; the ruler only moves markers.

---

## 11. New clips

### 11.1 The payloads

The clip library drags two MIME types, defined in `core/clips.py` so that `timeline/` never imports `ui/`:

| MIME | Payload | Carried by |
|---|---|---|
| `CLIP_MIME_TYPE` | `{"note", "velocity", "channel", "fixture_type"?}` | Channel verbs, blackout, chases, song parts, blackout all |
| `SCENE_MIME_TYPE` | `{"name", "channels": {field: 0–255}, "fixture_type"}` | A library scene |

The payload's `channel` is ignored: **the lane under the cursor decides the fixture**. `fixture_type` is present only
where the verb means something different on another fixture: a channel verb ("Tilt" is channel 1 on one fixture and
something else on another) and a chase (its velocity is an index into the source fixture's list). Such a clip drops
only on lanes of that type. Library cards drop at `DEFAULT_DROP_VELOCITY` 127, so a channel card sets 254.

### 11.2 Validation and insertion

`dragMoveEvent` and `dropEvent` ask the same questions:

- **`can_accept_clip_drop(note, y, fixture_type)`**: a song part is accepted anywhere (the section overlay decides
  where it lands). Anything else needs a lane under the cursor that is not locked, of the payload's type if it names
  one, on which `note_layout.note_applies_to(note, fdef)` holds: a channel verb within the fixture's channel count,
  scene and bank verbs only if it has banks, a chase verb only if it has chases, the rest anywhere.
- **`can_accept_scene_drop(y, fixture_type)`**: the lane's fixture has scene banks, matches the type, and is not locked.

A clip lands at `x / PIXELS_PER_SECOND` on the lane's channel, **unsnapped**. Its velocity is clamped to 1–127, or
0–127 on `VALUE_BEARING_NOTES`, where 0 is a real value: the first chase, channel at 0. `add_event` inserts it in
time order and reconciles the lane, so a drop inside a clip splits it there, and a drop exactly on a BLACKOUT removes
it. A scene drop calls `add_scene`, which **copies** the values into a manual scene (with its take-manual verb); it
does not follow later library edits. A song part goes to `SongSectionOverlay.apply_drop`, which snaps it to the beat
(Shift: the bar) and splits or retypes, and shows a ghost of the result while the drag moves (`preview_drop`).

`ContentView.dragLeaveEvent` consumes the leave instead of calling the base class. The view accepts drags without the
base class's own drag-enter bookkeeping, and the base `dragLeaveEvent` then warns about a leave before an enter, which
aborts the process on Wayland.

### 11.3 The add dialog

"Add event at this position" (an empty lane's menu, or a clip's) calls `on_add_event_requested(time, channel)`, which
opens `ClipEditDialog.for_clip(..., is_new=True)` and does nothing on a locked lane. The picker starts on "Select event
type…" and also offers "New scene (all channels)…", which switches the dialog to the scene page and ends in
`add_scene`. Anything else ends in `add_event` on the lane's channel.

---

## 12. The clip editor

`ClipEditDialog` edits one clip **against the fixture it lives on**, never against a table. `Timeline._fdef(channel)`
resolves the lane's patch entry to its `FixtureDef`, and everything the dialog offers follows from that: its channels,
banks, chases and preview. The header shows the fixture, its mode, its channel and DMX span.

It is opened modally from `on_midi_event_edit_by_id` (double-click, Edit Properties…) and from the add path, and it
returns a `ClipEditResult`. Its `kind` says which write to make:

| `kind` | Write |
|---|---|
| `"scene"` | `update_scene(group_id, channels, name, remove=removed)`, then `move_scene` if the time changed |
| `"new_scene"` | `add_scene(time, channels, name, channel)` |
| `"clip"` | `edit_event(event_id, note=, velocity=)`, then `move_event` if the time changed |

**The time is moved through `move_event` / `move_scene`, never `edit_event(time=)`.** Only the move searches for a free
slot and keeps the BLACKOUT markers honest. It can refuse, and the clip then stays where it was. The time box holds
exact seconds.

### 12.1 A single clip

The **EVENT TYPE** picker is `note_layout.verb_choices(fdef)`, grouped Channels / Scenes / Banks / Chases / Control /
Colour / Show. It is filtered by the same `note_applies_to` a drop is validated with, so the dialog can never author
a clip a drop would refuse, and by `_verb_resolves`, so a fixture with three scenes is not offered "Scene 9". Song
parts are left out: `edit_event` never clears a channel, so offering them here would make a channel-scoped song part.
A clip whose verb no longer applies (a mode change can do that) is shown as "Note n (not valid on this fixture)" with a
warning rather than silently retyped.

Under the picker sits the verb's own editor, chosen by `editor_class_for(note)` from a table whose predicates mirror
`handle_note_on`'s match arms:

| Verb | Editor | The velocity is shown as |
|---|---|---|
| 1–23 | `ChannelValueEditor` | A console fader 0–255, captioned with the capability at that value ("Gobo 3", "Strobe 4.2 Hz"), plus the preview card |
| 24–35, 57, 58 | `SceneLoadEditor` | The target scene's name and a blend time in seconds |
| 59 | `PackedSceneEditor` | Bank and scene pickers (only the first 8 scenes of a bank are reachable) |
| 52–54 | `TimeEditor` | Seconds, 0–12.7 |
| 70 | `ChaseEditor` | The fixture's chases by name |
| anything else | `NoParamEditor` | A one-line explanation; velocity 64 |

**Retyping resets the velocity** to the new class's default (`default_velocity_for`). Carried across, a channel at 200
(velocity 100) would select chase #100.

### 12.2 A scene

The scene page is the console: a `FaderStrip` over an `OutputCard`, whose preview is the stage engine fed the scene's
DMX, so a look previews exactly as on stage. A colour wheel appears when `wheel_applies(fdef)`, fitted to whatever the
fixture actually mixes; a channel whose capabilities name discrete choices opens a picker from its name.

- **Present, touched and removed.** The dialog tracks which channels the scene carries (`_present`), which the user
  moved (`_touched`), and which were removed. Only `present | touched` is submitted (`_result_channels`); an untouched
  channel stays out, since adding it at its default would force a value the scene never set. Channels not in the scene
  are shown dimmed until moved.
- **Only the first 23 slots are reachable** (24 and up is scene-load), so `ADDRESSABLE_CHANNELS` caps the result and
  the fader strip greys the tail with a status line saying so.
- **Load from library ▾** copies a stored scene onto the faders (every channel it names joins the clip). It reads
  through `load_scenes` directly, not the `SceneEditor` cache, which would still answer with the pre-save banks.
- **Save as scene…** writes the fixture **library** immediately, independent of OK and Cancel, because it edits a
  type every show shares. It asks for a bank (or a new one) and a name; an existing name can be overwritten in place
  (keeping the sidecar's other keys, such as `song_parts`) or added as a duplicate, since a saved `.mid` binds a scene
  verb by index. It emits `library_changed`, which the window uses to rebuild the clip library.

### 12.3 Live output

The **Live output** checkbox sends the dialled look to the rig while editing. It is never on by default and never
remembered, because the dialog can be open in front of an audience.

- Sends are coalesced to one per 30 ms (`LIVE_THROTTLE_MS`).
- A scene or a channel verb **leads with note 48 (take manual)**; otherwise the auto or song-part program overwrites
  it next frame. Other verbs are their own command and are sent alone.
- It is disabled while the transport plays (`Timeline.set_transport_playing`, told by the window), because the
  worker's own notes would fight it. `Timeline._on_clip_edit_live` also drops messages for a muted or locked lane.
  Messages go out as `preview_requested`, the same route as the clip library's audition.
- Cancel, or closing the window, **re-sends** the values the clip held when the dialog opened, once. That is a
  re-send, not a restore: it cannot put back what the automatic program was doing. OK leaves the committed look on the
  rig.

On a locked lane every control and OK are disabled, with a status line saying so. `closeEvent` clears both preview
cards: their bodies own timers on the stage's shared 30 Hz clock.

---

## 13. The auto-mode clips

When a lane's **A** toggle is on, a software controller exists and the fixture's scenes or chases carry `song_parts`
tags, the lane also shows what the song-part program will run (`_auto_markers`, `_draw_auto_scene_clips`).

- One `AutoSceneClip` per song-part marker that the fixture has something tagged for, from the marker to the next
  marker, or to the first manual clip that starts inside that section.
- Labelled with the first tagged scene or chase (" +N" when several are tagged, since the runtime pick is random), in
  the song part's colour, square-cornered, hatched and dash-bordered.
- Read-only and transparent to the mouse, beneath the manual clips (z 0.5). Recomputed on every redraw; never stored.
- On such a lane a manual clip is also cut short at the next song-part marker, because the program reprograms the
  fixture there.

---

## 14. The timeline's interface to the window

Signals out (all connected in `MainWindow.setup_ui`):

| Signal | Carries | Handled by |
|---|---|---|
| `seek_requested` | seconds | `seek_playback` |
| `play_from_requested` | seconds | `_play_from` (a section's "Play from") |
| `bpm_detected` | int | `bpm_state.set_bpm` (only on a load: `refresh_from_manager(detect_bpm=True)`) |
| `waveform_offset_changed` | seconds | Live offset preview |
| `waveform_offset_committed` | old, new seconds | The offset dialog (`docs/AUDIO.md` §7) |
| `offset_edit_requested` | | `open_offset_editor` (a band menu's "Set audio offset…") |
| `audio_/metronome_/voice_volume_changed`, `*_mute_toggled` | int / bool | `AppCore.set_volume`, the bus mutes |
| `track_heights_changed` | waveform, lane px | Persisted to `config.window` |
| `header_width_changed` | px | Persisted to `config.window` |
| `track_order_changed` | channels, top to bottom | Stamps `FixtureInstance.order`, saves the show |
| `zoom_changed` | m11 | The toolbar slider |
| `fixture_edit_requested` | channel | `open_fixture_editor` (deferred) |
| `library_changed` | | `_refresh_clip_library` |
| `preview_requested` | `[(channel, note, velocity)]` | `AppCore.preview_clip` |
| `marker_move_committed` | kind, old, new | `on_marker_moved` |
| `marker_edit_requested` | kind, time | `on_marker_edit_requested` |

`midi_data_changed` is declared and never emitted; the window listens to `MidiManager.data_changed` instead.

Calls in: `refresh_from_manager`, `rebuild_for_patch`, `set_tempo_map`, `set_bpm`, `set_voice_tts`,
`set_control_markers`, `set_song_tracks` (every audio track the song carries, so a short guide cannot trim the scene),
`set_files` (the monitored one), `set_audio_source`, `redraw_waveform` (when a decode lands), `set_waveform_offset`,
`update_playhead`, `set_transport_playing`, `set_ui_scale`, `set_volumes`, `set_zoom`, `fit_to_width`,
`set_ruler_mode`, `set_beat_division`, `set_snap_enabled`, `set_all_tracks_collapsed`, `set_midi_events_height`,
`refresh_auto_buttons`.

---

## 15. Rules not to break

### Writing

- **Every edit goes through `MidiManager`, and every editor op ends the same way**: mutate with the `_internal`
  helpers, `reconcile_blackouts(fixture)`, `regenerate_fixture(fixture)`, `_commit()`. Skip the reconcile and a
  zero-length BLACKOUT hides the next clip; skip the regenerate and a neighbour's fade-out stays where the old edge
  was.
- **Move clips with `move_event` / `move_scene`**, not `edit_event(time=...)`, which neither searches for room nor
  keeps the BLACKOUTs honest.
- **One user action, one undo step.** Anything that makes several manager calls wraps them in `history_batch()`.
- **Keep `midi_notes` in step, and find a clip's mirror by note, channel and time.** It is what the `.mid` and the
  worker are built from; a change only to `midi_events` shows on screen and is lost on the next rebuild, and a match
  without the channel can land on another lane's note at the same instant.
- **Notes 60 and 71 never become clips**, and nothing that shifts the show may move them.

### Item lifetime

- **From a scene item's handler, `_schedule_refresh()`, never `refresh_from_manager()`.**
- Retire scene items the defensive way (§5), and read what you need off an item before a menu's or dialog's `exec()`.
- Commit on release, and only after real movement (≥ 1 px for clips and handles, `startDragDistance` for sections,
  4 px for the waveform). A commit on a pure click destroys the item the click just selected.

### Gestures

- **A lone section box never moves.** Only a selection of two or more drags as a group.
- **A group move is one delta**, clamped by the intersection of the clips' room and the sections' room, committed in
  one batch from whichever grip took the drag.
- **Below the header bar, a section box ignores a plain left press**, so the waveform keeps its offset drag and
  click-to-seek.
- **Screen-pixel constants are divided by the zoom**; never compare a raw pixel radius with a scene distance.
- **Lanes bind by channel, display by row.** Never index a lane's fixture by its row.

### The clip editor

- **Only `present | touched` is submitted**, and growth and removal go through `update_scene(..., remove=)`.
- **Retyping resets the velocity.** Keep `verb_editors._TABLE`, `note_layout`'s ranges and `handle_note_on` in step.
- **Live output is opt-in, never persisted, locked out while playing or on a muted or locked lane, and leads with note
  48.** Cancel re-sends; it does not restore.
- **"Save as scene…" writes the library at once**; OK and Cancel are about the clip only.

---

## 16. Extending

**A new verb.** Add its range or constant to `core/note_layout.py`, label it in `describe_note`, gate it in
`note_applies_to`, place it in `_VERB_GROUPS`, and implement it in `handle_note_on`. If velocity 0 is a real value for
it, add it to `VALUE_BEARING_NOTES` (the codec and drops both read it). Give it an editor with a row in
`verb_editors._TABLE` if its velocity means something; otherwise `NoParamEditor` picks it up, with a line in
`_HINTS`. If a library card should drag it, add it in `core/clips.py`, with `fixture_type` set if its meaning depends
on the source fixture.

**A new lane gesture.** Preview in the item, commit on release after real movement, route through a `Timeline` method
that picks the scene or event API by `group_id`, and end with `_schedule_refresh()`. Put the maths in
`clip_geometry.py` so it can be tested without Qt.

**A new manager edit.** Build it from `MidiDocument`'s `_internal` mutators, scope it to one lane with
`_event_fixture`, and finish with the reconcile → regenerate → commit sequence. If it moves clips, use the
intended-ends technique (§3.4) rather than reasoning about which BLACKOUTs to keep.

**A new section rule.** Put it in `timeline/arrangement.py` on plain floats and test it there; the overlay should only
call it.

---

## 17. Tests

All headless, on the offscreen platform with the shared `QApplication` from `tests/conftest.py`; each file also runs
standalone through its `main()`. The document and editor tests build a patched `MidiManager`
(`MidiManager(note_layout.build_note_map(), fixtures=PATCH)`) and need no widgets at all.

| File | What it pins |
|---|---|
| `test_clip_geometry.py` | Zones, neighbour and grid snapping, interval clamping, move and resize resolution, `compute_bounds`. |
| `test_lanes.py` | Lane labels, lane order, channel → fixture lookups. |
| `test_timeline_tracks.py` | One lane per fixture, each showing only its channel's clips; song parts on no lane; drops routed by channel; marker flags committing on release. |
| `test_song_part_lane_scope.py` | A song part never occupies a lane, in the document or on screen. |
| `test_song_sections.py` | The arrangement rules and every strip gesture: boundaries, snapping, click and sweep, group moves, drops, the shared rubber band, the waveform keeping its press. |
| `test_clip_ops.py` | Single-clip ops: add, delete, move (including jumping a gap), left and right resize, replace with BLACKOUT; edit, retime and delete touching only their own lane's note when another lane holds the same note at the same time. |
| `test_scene_ops.py` | Scene groups: add, move, update (grow and remove), delete, resize. |
| `test_blackout_invariants.py` | The BLACKOUT invariants after a battery of mixed edits. |
| `test_fades.py` | Ramp maths, the fade setters and their clamping, ramps riding the fixture's own dimmer. |
| `test_copy_paste.py`, `test_paste_chain.py` | The clipboard, and repeat-paste chaining. |
| `test_undo_redo.py` | The snapshot history: baseline, walk-back, redo truncation, batches; undo and redo re-seeding the controller's auto flags. |
| `test_midi_fidelity.py` | What the `tdmx1:` metas carry across a save: scene names, stable group ids, clip fades. |
| `test_clip_edit_dialog.py` | The clip editor: its own fixture's channels, present-or-touched submission, the picker matching `note_applies_to`, the 23-slot cap, the velocity reset on retype, preview cleanup. |
| `test_clip_edit_baseline.py` | Clip editing as it stood before the generalized dialog, with each changed behaviour marked. |
| `test_auto_scene_viz.py` | The auto-mode clip helpers (`song_part_refs`, `song_part_clip_info`, the clips drawn). |
| `test_note_layout.py`, `test_clips.py` | `describe_note`, `note_applies_to`, `build_note_map`'s order; the clip library's groups and payloads. |
| `test_crash_repro.py` | Real mouse events through the view: a commit from a clip's release must not destroy the item under Qt (the deferred refresh). |
| `test_drag_leave_crash.py` | A stray drag-leave over the view must not abort. |
| `test_tooltip.py` | The tooltip is a child widget, clamped inside the window and transparent to the mouse. |
| `test_offset_ripple.py` | The show following the audio when the offset moves (`shift_all`). |

---

## 18. Known limits and quirks

- **A clip edit in the dialog can be two undo steps.** `edit_event` (or `update_scene`) and the following
  `move_event` (or `move_scene`) each commit, and they are not batched.
- **A BLACKOUT cannot be selected or removed directly**, because it is never drawn. A gap closes by resizing the clip
  before it to the right, or the clip after it to the left. The "Remove blackout" menu entry and the
  `is_blackout_clip` paint branch in `DraggableMidiEvent` are unreachable, since `redraw_events` never builds a clip
  with `is_blackout=True`.
- **BLACKOUT ALL dropped on a lane is invisible.** Its part name starts with BLACKOUT, so it is a gap marker on that
  lane: what shows is the clip before it ending there.
- **Lane drops do not snap**, though song-part drops do and clip drags do.
- **Alt+←/→ needs a section in the selection.** With only clips selected, Alt+arrows seek.
- **On a song shorter than four minutes the last clip runs past the scene.** `song_end_time()` has a 240 s floor, and
  a lane draws its last clip to that × 1.05, beyond the scene's right edge.
- **The section tint is white.** `WAVE_TINT_MIX` is 0.0, so `_wave_tint` returns white for every part, which is the
  waveform's own colour: the per-section recolour runs on every paint and changes nothing.
- **Stale comments.** `midi_events.py`'s module docstring says gaps are drawn as transparent BLACKOUT clips;
  `verb_editors.py` names `editor_for(note, fdef)` (the function is `editor_class_for(note)`); the codec says fade
  durations are "not persisted to the MIDI file" (the `tdmx1:` metas persist them); `MidiManager.is_song_part` says
  song parts show on every fixture track (they show on none); `SceneEditor.add_scene` describes a "7-event LED scene";
  `ClipEditDialog.live_messages` says "Phase 5 wires this" (it is wired); `midi/constants.py` says the timeline never
  imports `midi/` (§2, Layering).
