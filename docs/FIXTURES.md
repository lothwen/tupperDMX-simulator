# Fixtures

*A fixture type is an Open Fixture Library document, read as published. What OFL has no room for (scene banks,
chases, song-part tags, two knob flags, render hints, effect artwork) rides beside it in a `.tdmx.json` sidecar and an
`.effects/` folder. Nothing in the app is written per fixture: what a channel does, how it draws, which controls edit
it and which clips it offers all come from the capabilities its channels declare.*

This document covers the fixture library (`core/ofl.py`, `core/fixture_sidecar.py`, `core/fixture_library.py`), the
photometric model (`core/fixture_state.py`), the fixture-editing UI (`ui/fixture/`: `AddFixtureDialog` and its tabs,
the console, the affordances and the stage designer's inspector), and the clip model and favourites (`core/clips.py`,
`ui/clip_library.py`). It does not cover how the stage draws a fixture (`docs/STAGE.md`), what a clip does once it is
on the timeline (`docs/TIMELINE.md`), or how the software controller plays scenes and chases.

Written 2026-10-04 against `betatest1-fixes` (commit `f33c001`).

---

## 1. What it does

From files to light:

1. `load_fixture_defs()` reads three layers of fixture folders (shipped, user, the open show's) and registers each OFL
   document under a key, paired with its sidecar.
2. `resolve_fixture(type, mode)` turns one registered type and one mode into a `FixtureDef`: an ordered list of DMX
   slots (`ChannelSpec`s, each carrying its OFL capabilities) plus that mode's banks, chases, effect artwork and stage
   hints.
3. A patch entry (`FixtureInstance`: type, mode, `dmx_start`, MIDI `channel`, `hanging`) places one `FixtureDef` in
   the universe. The software controller compiles it, the stage builds bodies for it, the timeline gives it a lane and
   the clip library a section.
4. Every DMX frame, `resolve_state(fdef, slice)` says what the fixture emits. `solve_color(fdef, rgb)` is the inverse,
   used by the palettes and the colour wheel.

Editing runs the other way: the Scenes and Chases tabs rewrite the sidecar, never the OFL document, and the write
invalidates the registry, so the controller recompiles and every reader sees the new scenes without a restart.

```
fixtures/ (shipped)    ─┐
<userprefs>/fixtures/  ─┼─ load_fixture_defs ── {key: FixtureDef at its default mode}
<Show>.show/fixtures/  ─┘   later layer wins by key
                                  │
                                  ▼
                 resolve_fixture(type, mode) ── FixtureDef (one mode)
      ┌──────────────────┬───────────┴────────┬─────────────────────┐
      ▼                  ▼                    ▼                     ▼
fixture_type_for    stage bodies +       note_layout            clips.build_groups
(controller,        resolve_state        (what notes apply;     (library cards,
 compiled per        every frame          timeline, editor)      favourites, remote)
 cache generation)
      ▲
save_fixture_banks / save_fixture_chases ── sidecar written ── clear_cache(): generation + 1
```

---

## 2. Where the code lives

| File | Qt | What it does |
|---|---|---|
| `core/ofl.py` | none | The OFL 12.5.1 parser: channels, capabilities, entities and units, wheels, the matrix and template channels, switching and fine channels, `resolve_mode()`. Metadata is parsed for display only. |
| `core/fixture_sidecar.py` | none | The `.tdmx.json` parser (`Sidecar`, `ModeExtras`, `for_mode`) and the raw-JSON writers (`banks_mode_key`, `apply_banks`, `write_sidecar`). |
| `core/fixture_library.py` | none | The registry: layers, keys, `FixtureDef` / `ChannelSpec`, `resolve_fixture`, `fixture_modes`, `fixture_document`, `effect_asset`, the capability lookups, the two writers and the fork, the per-type colours. |
| `core/fixture_state.py` | none | `resolve_state` (DMX → `FixtureState`), `solve_color` (RGB → DMX), `capability_at`. |
| `core/channel_families.py` | none | A channel's one-word family and colour, from its dominant capability: the browser's chips, the fader colours, channel-clip labels. |
| `core/note_layout.py` | none | Which notes a fixture answers to (TIMELINE.md §3.2), and `song_part_refs`, the sidecar's song-part tags indexed for auto mode. |
| `core/clips.py` | none | Clip descriptors, the drag MIME types and payloads, refs, favourites resolution. |
| `software_controller/scene_loader.py` | none | `load_scenes(type, mode)`: a type's banks as `Scene` records. |
| `software_controller/main.py` | none | `fixture_type_for(type, mode)`: the compiled-type cache, rebuilt when `cache_generation()` moves. |
| `ui/fixture/browser.py` | QtWidgets | `AddFixtureDialog` (add and edit modes): the library tree, the OFL detail tabs, the patch footer. `used_dmx_spans`, `used_midi_channels`, `suggest_free_dmx_start`. |
| `ui/fixture/scenes_tab.py` | QtWidgets | The Scenes tab: a type's banks. |
| `ui/fixture/chases_tab.py` | QtWidgets | The Chases tab: a type's chases. |
| `ui/fixture/palette_tab.py` | QtWidgets | The Palette tab: one instance's note 60 in the loaded song. |
| `ui/fixture/console.py` | QtWidgets | `FaderStrip`, `OutputCard`, `ScenePreview`. Shared with the timeline's clip editor. |
| `ui/fixture/affordances.py` | QtWidgets | `ColorWheel` and `CapabilityGrid`. Shared with the timeline's clip editor. |
| `ui/fixture/designer.py` | QtWidgets | The stage edit toolbar (add fixture, haze, exposure, fog) and `FixtureInspector`. |
| `ui/clip_library.py` | QtWidgets | The clip library panel: group pills, cards, stars, the two routes into the fixture editor. |
| `ui/main_window.py` | QtWidgets | `open_fixture_editor` (the one way the edit dialog opens) and `_on_fixture_selected`. |

### Layering

- **The four `core/` modules are Qt-free.** `ofl` and `fixture_sidecar` know nothing about each other or the app;
  `fixture_library` combines them; `fixture_state` reads the resulting `FixtureDef`.
- **The library never imports the controller.** That would be a cycle (the controller imports `core.config`, which
  imports the library), so the controller polls `cache_generation()` instead of being told about a reload.
- **`core/clips.py` is Qt-free because three widget-free consumers need the same answer**: the library panel renders
  its cards, the timeline decodes its drop payloads, and the web remote resolves the show's favourites. A second
  implementation of "what does this scene fire" is a second answer waiting to disagree with the first.
  `CLIP_MIME_TYPE` lives there, which is what stops `timeline/` importing `ui/`.
- **`ui/fixture/` is the middle layer of `ui/`**: it knows the app (config, the stage controller, `MidiManager`) but
  not the window. The timeline's clip editor reaches into `console` and `affordances` and nothing else.
  `ui/fixture/__init__.py` re-exports nothing.

---

## 3. A definition on disk

### 3.1 One or two files, plus an optional folder

| File | Holds | Written by the app |
|---|---|---|
| `<stem>.json` | Strict OFL: the product. Channel count and order, capabilities, physical data, matrix, wheels, modes. | Never. |
| `<stem>.tdmx.json` (optional) | Authoring data per mode: scene `banks`, `chases`, per-channel `label` and `flags`, the `effects` artwork map, `stage` hints. | `banks` and `chases` only. |
| `<stem>.effects/` (optional) | The artwork the `effects` map names. | Never (copied by a fork, §6.1). |

A definition straight from OFL works with no sidecar; it simply has no scenes or chases. The OFL root forbids extra
properties, which is why the authoring data cannot ride inside it. Anything that travels with a definition (a
`.showpack`, the fork into the user layer) carries all three.

**A sidecar can never change the DMX footprint.** Channel count, order and addressing come only from the OFL document,
because a saved `.mid` binds note N to the N-th channel.

### 3.2 Layers and keys

`load_fixture_defs()` reads three layers in order: the shipped `fixtures/` (`bundle_dir()`), the user layer
`userprefs_dir()/fixtures/`, and the open show's `fixtures/` (`set_show_fixtures_dir`, called when a show loads). A
later layer replaces an earlier one's definition by key, with its source record, so an overridden key cannot resolve
modes from the old document. The result is memoised; `clear_cache()` drops it and bumps `cache_generation()`.

- **A subfolder is the manufacturer, and it namespaces the key.** `fixtures/ADJ/focus-spot-4z.json` registers as
  `"ADJ/Focus Spot 4Z"`: the folder, then the sidecar's `key` or else the OFL `name`. Two makers can ship a fixture of
  the same name without one replacing the other. Deeper nesting keeps the whole relative path.
- **A file directly in a fixtures folder keeps its bare key**, which is what existing patches refer to. That is also
  why artwork lives in `ledbar.effects/` and never in `ledbar/`: a plain subfolder would be read as a manufacturer.
- **A sidecar pairs only with a document in its own folder** (its `fixture` key, default `<stem>.json`). One that
  targets nothing beside it is ignored with a warning, and **sidecars never cross layers**.
- A document that is not OFL (no `name`, `categories`, `modes`, or channels) is skipped with a warning naming the
  missing key. A patch entry whose type no longer resolves is dropped from the show on load, also with a warning
  (`Config._parse_fixtures`).

### 3.3 The sidecar

```json
{
  "tdmx": 1,
  "fixture": "ledbar.json",
  "key": "ledbar",
  "default_mode": "7ch",
  "modes": {
    "*":   { "channels": {}, "banks": {}, "chases": [], "effects": {}, "stage": {} },
    "7ch": { }
  }
}
```

- `tdmx` (the format version) is required. A sidecar newer than the app knows is ignored whole.
- **`channels` merges per key; everything else replaces wholesale** (`Sidecar.for_mode`). A named mode that declares
  `banks`, `chases`, `effects` or `stage` replaces the `"*"` block's, because a bank set only makes sense as a whole. A
  named `stage` block must therefore repeat every key it still wants.
- `channels`: per resolved channel key, a `label` (a display name, for a channel whose capabilities describe it too
  vaguely, such as two `Generic` ranges that are really a direction) and `flags` (§3.4). **What a channel does is never
  declared here.** The obsolete `role` key is ignored with a warning.
- `banks`: `{bank name: [scene, …]}`, where a scene is `{"name", <channel field>: value, …, "song_parts": […]}`.
  **Order is semantic**: note 36+n selects the n-th bank, the first bank is the fixture's boot bank, and note 24+n loads
  the n-th scene of the current bank. A channel a scene leaves out is not part of it.
- `chases`: `[{"name", "banks": […], "scenes": […], "song_parts": […]}]`; step n is scene `scenes[n]` of bank
  `banks[n]`. Note 70's velocity selects a chase by index, so their order is semantic too.
- `song_parts` tags on scenes and chases are what auto mode runs per song part (`note_layout.song_part_refs`). An
  untagged chase can only be fired by hand.
- `effects`: `{effectName: {"asset": "01-ON.gif"}}`, keyed by an `Effect` capability's own `effectName` (`gif` is the
  older spelling, still read). `effect_asset(fdef, name)` resolves it against **this definition's own** `.effects/`
  folder: artwork is per fixture, never matched by name across all of them, so two fixtures may each have an
  `01-ON.gif`. A missing file degrades to showing the effect's name. It hits the filesystem, so a render path resolves
  it when the effect changes, not per frame.
- `stage`: render hints (`size_px`, `bodies`, `body_spacing`, `cells`, `beam`), read by `stage/` (`docs/STAGE.md` §17).

### 3.4 Channel flags

`ChannelSpec.flags` carries three behaviours the controller needs and no capability implies (`FLAG_NAMES`, mirroring
`CH_*` in `software_controller/main.py`):

| Flag | Bit | What the controller does |
|---|---|---|
| `blend` | 0x01 | Interpolates the channel over the blend time instead of snapping. |
| `knob_level` | 0x02 | Caps it by the brightness knob at scene load. |
| `knob_speed` | 0x04 | Takes it from the velocity, the speed knob or the scene. |

`blend` is derived from the dominant capability (§4.2): continuous ones (intensity, colour, zoom, pan and tilt,
rotations, fog …, `_BLEND_CAPABILITIES`) blend; wheel slots, effects and mode selectors cut, because interpolating them
would sweep through every program or gobo in between. `EffectSpeed`, `Speed` and `Effect` are left out on purpose: the
shipped blinder derives its flags from that table, and adding them would change real controller output.

**A sidecar `flags` list replaces the derived set wholesale.** The knob flags cannot be derived (two fixtures'
brightness channels behave differently), so a sidecar that mentions flags states all of them.

---

## 4. From OFL to `FixtureDef`

### 4.1 Resolving a mode (`core/ofl.py`)

`parse_ofl` flattens every mode into a `ResolvedMode`: one `ResolvedChannel` per DMX slot, in address order, with

- **matrix inserts expanded**: template channels for every pixel and pixel group, `repeatFor` honoured;
- **fine channels linked** to their coarse slot (`fine_of`, `fine_shift`), so a 16-bit value folds into one number;
- **unused and unresolvable slots kept in place**: they still consume an address, and dropping one would shift every
  channel after it, and every note binding with them;
- **switching channels** (`switchChannels`) held as the channel active at the trigger's default value, with every
  option kept in `switch_options` for readers that resolve the live one.

`resolve_mode(fixture, name)` looks a mode up by name or short name, case-insensitive; an empty or unknown name falls
back to the first declared mode. The mode's `physical` overrides the fixture's key by key.

The parser is a consumer, not a validator: unknown capability types keep their properties, unknown keys are ignored,
and only a document that is not an OFL fixture at all is refused. Conformance is checked by the tests, against OFL's
own schema (§11).

### 4.2 The bridge (`build_fixture_def`)

One `ChannelSpec` per slot. It keeps the OFL channel's capabilities, which are **the only description of what the
channel does**: there is no summarised channel description and no role enum anywhere. A placeholder slot gets the field
`__unused_<n>` and no capabilities.

A channel can mix capability types across its range. `dominant_capability()` names the one covering the most DMX,
ignoring `NoFunction`, with ties broken by `_TYPE_PRIORITY` (what the light is, then what it passes through, where it
points, the air, the program). Two capabilities only add up if they share type, colour, wheel and blade, so a channel
that is half colour wheel and half gobo wheel is not one 100 %-wide wheel channel.

**The default mode.** `resolve_fixture(type, "")`, and a patch entry with `mode: ""`, resolve to the sidecar's
`default_mode`, else the first mode the document declares. A patch stores `""` for a single-mode type, and an unknown
mode falls back to `""` on load, so renaming a mode in a definition never strands a patch.

The lookups everyone shares, all reading capabilities:

| Function | Answers |
|---|---|
| `color_slots(fdef)` | OFL colour name → the first slot driving it. |
| `intensity_slot(fdef)` | The master dimmer's slot. Fades ride it (TIMELINE.md §3.6). |
| `capability_types` / `all_capability_types` | The types a fixture declares: dominant per channel / anywhere in a range. |
| `fixture_modes(type)` | The mode names, in order. Mode pickers appear only when there is a choice. |
| `fixture_document(type)` | The parsed OFL document, for the browser's metadata and modes. |

---

## 5. The photometric model (`core/fixture_state.py`)

`resolve_state(fdef, dmx)` answers "what is this fixture emitting?" for one frame. It is Qt-free and serves every
renderer (the stage, the scene preview, the auto program's replay), so what is resolved here is what is drawn.

1. **Fold** the slice: fine bytes into their coarse value. A slot the slice does not reach takes the channel's default,
   so a short frame reads as "not set yet", not as a blackout.
2. **For each slot**, find the capability in force (a switching channel reads its trigger's live value first) and hand
   it to its handler. `_HANDLERS` maps all 43 OFL capability types; `NoFunction`, `Generic` and `Maintenance` emit
   nothing by definition.
3. **Freeze** into a `FixtureState` of small records: `color` (with per-cell `emitters`), `optics`, `motion`, `strobe`,
   `program`, `atmosphere`, `wheels`. The flat names from before the groups existed (`rgb`, `shutter`, `effect`,
   `zoom_deg` …) remain as read-only views.

**Emitters**, a fixture's independently lit cells, come from three sources: matrix channels naming their pixel, matrix
groups commanding several pixels, and **repeated channels**, where the n-th channel of one colour (or the n-th plain
dimmer) belongs to cell n. That last rule is how a multi-cell blinder (cool1, cool2, warm1, warm2) resolves into two
cells with no per-fixture code. A `stage.cells` hint raises the count, so the cells exist before any DMX arrives. With
no dimmer at all a fixture is always on; with no colour channel it is white unless a colour temperature tints it; a
colour wheel then filters what it has.

**`capability_at(fdef, index, values)` is the one "what is this channel doing now"**, and it takes the whole slice: a
switching channel's meaning lives on another slot, so asking with one value describes the wrong channel.

**`solve_color(fdef, rgb)` inverts the same additive model** (`REFERENCE_RGB`), so what the controller writes is what
the stage draws:

- with Red, Green and Blue: the components go straight there, and every other colour slot is zeroed so a leftover amber
  cannot tint the new colour;
- with another chromatic set (CMY, RGBA without blue …): a greedy fit, largest contribution first;
- with whites only, or no colour at all: `{}`. A palette is a hue, and a blinder has none.

The controller's palettes and the clip editor's colour wheel both write through it. The model is additive even for CMY:
being consistent with the renderer was chosen over being right about a dye wheel.

---

## 6. Editing a type's scenes and chases

The Scenes and Chases tabs edit the fixture **library**: a type's sidecar, shared by every show that uses the type.
They sit inside `AddFixtureDialog`, but **its OK and Cancel do not apply to them.**

### 6.1 The writers

- **`save_fixture_banks()` and `save_fixture_chases()` are the only writers**, and between them they touch `banks` and
  `chases` and nothing else. The OFL document is never written.
- They round-trip the **raw** sidecar JSON (`apply_banks` on a deep copy) rather than re-serialising the parsed
  `Sidecar`, so unmodelled keys (a newer version's, a field added by hand) survive an edit.
- **Banks and chases are written separately.** The two tabs each hold their own copy of the block, so a write carrying
  both would put the other tab's unsaved work on disk; `None` for either half leaves it as it is on disk. The one
  crossing is deliberate: renaming a bank rewrites the chases that name it. After either tab writes,
  `AddFixtureDialog._on_library_changed` reloads the other tab's copy.
- `banks_mode_key` picks the mode block to write: whichever already holds the banks; otherwise `"*"` for a single-mode
  fixture and the named mode for a multi-mode one, since two modes do not share a channel set.
- **A definition in a folder that is not persistable is forked into the user layer first** (`_fork_to_user_layer`):
  the OFL file, the sidecar and the `.effects/` folder are copied under the same manufacturer folder and names, so the
  fork registers under the identical key and, as a later layer, replaces the shipped one. A frozen build's shipped
  folder lives under PyInstaller's `_MEIPASS`, which is writable and deleted on exit, so it counts as read-only
  whatever its permissions say.
- An existing sidecar that cannot be read is refused rather than overwritten.
- Every write ends in `clear_cache()`, which bumps `cache_generation()`; the controller's `fixture_type_for` recompiles
  on its next lookup, so a new scene is live without a restart.

### 6.2 The Scenes tab

- The list keeps **bank sections with scenes beneath**, never one flat list, because both orders are what MIDI
  addresses.
- **A reorder is applied to the data, never by letting Qt move the items**, whose roles would go stale.
- It opens on a blank **draft** ("Create"). Clicking a scene loads it ("Save" overwrites it). **Save as new** writes
  the edits as the next scene and puts the original back, so a bank of variations builds itself.
- **The Save button carries exactly one scene: the one on the faders.** Everything that changes the list (new,
  duplicate, delete, drag, renaming a bank) is written as it happens. Stepping onto another scene, leaving the fixture
  or closing the dialog asks whether to keep unsaved fader edits, and "No" puts the scene back.
- **Adding, deleting or reordering a scene shifts the indices after it, and a saved `.mid` binds a scene verb by
  index.** The tab cannot hide that. Renaming a bank is safe.

### 6.3 The Chases tab

- A step is **(bank NAME, index in that bank)**, as the runtime reads it, so a chase survives a bank rename and not a
  scene reorder. The picker offers "BANK · Scene".
- The list and the Save button work as in the Scenes tab. There is no blank draft: "+ New" adds a real chase whose
  first step already points at a scene.
- **`CHASE_TIMING_ENABLED` is off.** The model carries per-step fade, hold and level and per-chase direction and units
  (`normalise_chase` / `denormalise_chase` keep them through a save), but `update_chase` runs every chase forwards on
  one per-fixture chase time (note 54). With the flag off the tab builds none of those controls, and Run steps at the
  engine's own `DEFAULT_CHASE_TIME` / `DEFAULT_BLEND_TIME`.
- **SONG PARTS is the one control the runtime honours in full**: `song_part_refs` reads the tags off a chase as it does
  off a scene.

The timeline's clip editor also writes the library, through the same writer, with **Save as scene…** (TIMELINE.md
§12.2); that write too is independent of the editor's own OK and Cancel.

---

## 7. Patching: `AddFixtureDialog` and the stage designer

### 7.1 Two modes, one dialog

`AddFixtureDialog` asks which type, which mode and where it goes. **`instance=` is the whole difference between its two
modes:**

- **Add** (no instance). The library tree, grouped by manufacturer or by OFL category, picks a type; the footer
  proposes the first address past the patched channels and the next free MIDI channel.
  `StageEditToolbar.open_add_fixture` (also the timeline toolbar's "+ Add track") runs it, patches what `values()`
  returns at the stage centre, and only then applies the palette, because the channel it belongs to exists only now.
- **Edit** (`instance=` a patched fixture). The library rail is hidden, since a patched fixture cannot become another
  type, and the header, tabs and footer point at that instance. It adds a name, the rigging switch and Delete.

**In edit mode nothing is written until OK.** Name, address, channel, mode and rigging are answers held in widgets;
`apply_edits()` writes them after an accepted dialog and reports whether anything moved. Mode and rigging are rebuilds
(`change_fixture_mode`, `set_fixture_hanging`), not writes. Delete is confirmed, removes the fixture by identity and
closes the dialog. Whichever button closes it, `done()` settles the library tabs (asking about unsaved edits) and
clears both previews.

The tabs: **General**, **Channels** and **Modes** render the OFL document (physical data, metadata and links, each
channel with its family chip and capabilities, every mode); **Scenes** and **Chases** are §6; **Palette** is §7.4.

### 7.2 The patch decision

The footer is the patch decision, validated live:

- **DMX**: the span `[start, start + channel count − 1]` must not run past 512 or overlap another fixture
  (`used_dmx_spans`, which leaves out the instance being edited, by identity, or it would collide with itself).
- **MIDI channel**: must be unused (`used_midi_channels`). **The channel is the fixture's identity for every saved
  clip**, so a duplicate is refused, never silently reassigned.
- **OK stays disabled until both are legal.**

The designer's `FixtureInspector` refuses the same two, through the same helpers so the two surfaces cannot drift,
**one field at a time**: a typed address or channel that is illegal is warned about and the old value put back. A mode
change is allowed even when the wider layout lands on a neighbour, and the card then carries a standing warning until
the address moves. A DMX clash is rig-wide, so the timeline's lane headers show it too (TIMELINE.md §9.1).

### 7.3 Opening the edit dialog

**`MainWindow.open_fixture_editor(channel, tab, ref)` is the one way the edit dialog opens, and it defers itself with a
zero timer.** All three entry points (a timeline lane header, a clip card's menu, the clip library's `+ New`) emit
from inside a widget that an edit in the dialog destroys: a patch change rebuilds the timeline's headers and the clip
library's cards with `deleteLater()`, and a modal `exec()` is an event loop that runs those deletions, so a dialog
opened straight from the emitting handler returns into a freed C++ object. A second request while one is open raises
the existing dialog. `reveal(ref)` opens on the scene or chase a card names, not merely on its tab.

**`MainWindow._on_fixture_selected` is gated on edit mode, not on the emission.** `change_fixture_mode` and
`set_fixture_hanging` also emit `fixture_selected`, and the edit dialog drives both from the timeline and the clip
library with the stage nowhere near the designer.

### 7.4 The Palette tab

Shown only for an OFL **Color Changer** (`palette_tab.applies_to`). It edits note 60 for **this instance in the loaded
song** (hue × 6 + mode), read and written through `MidiManager.fixture_palette` / `set_fixture_palette`: song-scoped,
like the `.mid`, and hidden when no song is loaded. Unlike the library tabs it is one of the dialog's answers, so it
waits for OK (`apply_edits` in edit mode, `apply_palette(channel)` after an add). Note 60 as per-fixture state is
TIMELINE.md §3.7.

### 7.5 The console and the affordances

- **`console.py`**: a `FaderStrip` (one fader per channel, coloured by family, captioned with the capability at its
  value) over an `OutputCard` holding a `ScenePreview`. **`ScenePreview` is the stage engine** (`renderer_for`, one
  body per `body_count()`, `docs/STAGE.md` §16) fed raw DMX, so a look previews exactly as on stage. **`clear()` it
  before it goes away**: its bodies hold timers on the stage's shared clock and shared movies.
- **`affordances.py`**: two controls that beat a fader, both chosen from capabilities and never from a fixture's name.
  `ColorWheel` writes through `solve_color`, so it drives whatever the fixture mixes. `CapabilityGrid` lays out a
  channel of discrete `Effect`, `ColorPreset` or `WheelSlot` capabilities as tiles, all animating at once; a click
  writes the start of that capability's `dmxRange`. Tiles show the fixture's own artwork (`effect_asset`), else the
  capability's name.

---

## 8. The clip model and favourites (`core/clips.py`)

**A clip is derived, never stored.** `build_groups(config)` regenerates every descriptor from the patch plus the
library, so a repatch or a sidecar edit shows on the next rebuild. A descriptor is plain data (`title`, `type`,
`accent`, `mime_type`, `payload`, `preview`, `sentinel`, `ref`).

The groups, per patched (type, mode) in DMX order, all in the type's colour (`color_for_type`, shared with the
timeline's lane swatches):

- the fixture's own verbs: channel controls, blackout, colour macros;
- one group per scene bank, labelled after the bank;
- a Chases group;

and one global **Show** group (the song parts and blackout all).

**A per-fixture group carries `fixture` and `mode`; the Show group carries neither.** A bank group is labelled after the
bank, so its label cannot say which fixture it came from. "Which fixture is this group about?" has to be answerable
rather than inferred, and the clip library's `+ New` and card menus both turn on it.

A (type, mode) group fires at the **first patched instance of that type and mode, by DMX address**
(`fixture_index_for_type`), and the card menu names the instance it will open. The drag payloads and how the timeline
validates them are TIMELINE.md §11.

### 8.1 Refs and favourites

A favourite is a starred card. Favourites are **show content** (`show.json`'s `favorites`, toggled through
`ShowManager.toggle_favorite`): a new show starts with none, and there is no machine-level fallback. The web remote's
cue grid is the show's favourites (`clips.resolve_all`).

- **A favourite stores a symbolic `ref`, never resolved MIDI.** Stored messages would freeze a scene's values at the
  moment it was starred and address the wrong fixture after a repatch; a ref re-resolves through the same builder the
  library renders, so a favourite fires what its card shows. There are three kinds: `note` (type, mode, note,
  velocity; a chase is note 70 with its index), `scene` (type, mode, bank, name, index) and `show` (note, velocity).
- **A scene keys on `(bank, name)`, with its index as a tiebreaker only.** The Scenes tab reorders in the data, so an
  index-keyed favourite would start firing a different look, while a name-keyed one merely goes unresolved on a
  rename. The index earns its place because names repeat within a bank (the shipped LED bar pads FILLS2 with four
  identical rows). `resolve_in` retries a missed scene on (fixture, mode, bank, name) alone, which carries a favourite
  across a reorder.
- **Resolution failure is a display state, never a delete.** An unresolved ref comes back as `None`, shown dimmed and
  unfireable, with `describe_ref` saying what it was and why it is dead, because repatching the fixture has to bring it
  back.
- `parse_favorites` is lenient: a malformed entry is dropped, and duplicates collapse keeping first-seen order, which is
  the grid order.

---

## 9. Rules not to break

### Definitions

- **What a channel does comes from its capabilities, and what a fixture is from its OFL categories; never from its type
  name.** No code anywhere is written per fixture.
- **Never write the OFL document, and never let a sidecar change the DMX footprint.**
- **Keep unused and unresolvable slots in place.** They hold addresses, and note bindings count them.
- **Write banks and chases only through `save_fixture_banks` / `save_fixture_chases`, separately, on the raw JSON.**
- **Never write into a folder that will not persist**; fork into the user layer.
- **Resolve artwork per fixture**, through `effect_asset`.
- **Call `clear_cache()` after changing a definition on disk**, or the controller keeps its compiled types.
- **Bank order, scene order within a bank and chase order are semantic.** Reordering changes what saved `.mid` files
  fire.

### Patching

- **The MIDI channel is a fixture's identity**: unique, refused when duplicated, never reassigned. Find a fixture by its
  `channel`, never by its position in the patch.
- **A patch stores `mode: ""` for the default mode.**
- **The footer keeps OK disabled until the patch is legal, and the inspector refuses the same two problems through the
  same helpers.**
- **Edit mode writes nothing until OK; the library tabs ignore OK and Cancel.**
- **Open the edit dialog only through `MainWindow.open_fixture_editor`.**
- **`clear()` a `ScenePreview` before it goes.**

### Clips and favourites

- **Derive clips; never store them.** Favourites store refs, never MIDI.
- **A scene ref keys on its name; the index only breaks ties.**
- **An unresolved favourite is shown, never deleted.**
- **`core/clips.py` stays Qt-free.**

---

## 10. Extending

**A new fixture.** Drop the OFL document (from the OFL project or its editor) into a fixtures folder: shipped, the user
layer, or a show's `fixtures/`. Put it in a manufacturer subfolder unless an existing patch refers to a bare key. It
works as is; add a `.tdmx.json` only for scenes, chases, labels, knob flags, artwork or stage hints. For a shipped
fixture, `test_ofl_schema.py` validates it against OFL's schema and `test_ofl_parity.py` pins its resolved shape, so
update the golden data on purpose.

**A new capability type.** Add a handler to `fixture_state._HANDLERS` (`test_capability_coverage.py` fails until every
OFL type resolves), decide its place in `_BLEND_CAPABILITIES` and `_TYPE_PRIORITY`, name its family in
`core/channel_families.py` if it needs one, and draw it (`docs/STAGE.md` §19).

**A new sidecar key.** Parse it in `_parse_mode_extras`, decide whether `for_mode` merges or replaces it, and expose it
on `FixtureDef`. The writers already preserve it, since they round-trip the raw JSON.

**A new clip kind.** Build it in `core/clips.py` with a ref constructor, extend `ref_key`, `parse_favorites` and
`describe_ref`, and give it a payload the timeline's drop validation understands (TIMELINE.md §16).

---

## 11. Tests

All headless; each file also runs standalone through its `main()`. `tests/ofl_factory.py` builds synthetic OFL
documents, so a capability or category no shipped fixture has can still be exercised.

| File | What it pins |
|---|---|
| `test_ofl_parser.py` | Mode resolution (null slots, fine channels, matrix inserts), capability range lookup, pixel ordering, entities and units. |
| `test_ofl_schema.py` | Every shipped definition is valid OFL against the schemas vendored in `tests/data/ofl_schemas/` (tag `schema-12.5.1`), and every sidecar pairs with a real document and restates nothing it owns. |
| `test_ofl_bridge.py` | The OFL → `FixtureDef` bridge and the sidecar: colour slots by OFL colour name, flags, labels, mode merging. |
| `test_ofl_parity.py` | The shipped types as the rest of the app sees them (channel order, flags, banks, chases, note map, song-part program), against golden data. |
| `test_fixture_library.py` | The three layers and how a definition's files pair within a layer; built-in types reaching the controller. |
| `test_fixture_modes.py` | A patched mode reaching the channel count, the notes, the controller's compiled type and the stage. |
| `test_fixture_state.py` | The photometric model, including parity with the renderers it replaced. |
| `test_capability_coverage.py` | All 43 OFL capability types resolve somewhere a renderer can read. |
| `test_fixture_scenes.py`, `test_fixture_chases.py` | The two tabs and the sidecar writes behind them: banks as sections, writes per half, bank renames following into chases, the fork. |
| `test_fixture_palette.py`, `test_palette_lifecycle.py` | Note 60 as per-fixture state; the palette's lifecycle on the controller. |
| `test_add_fixture_dialog.py` | Manufacturer namespacing and the browser built on it. |
| `test_edit_fixture_dialog.py` | Edit mode: the hidden rail, nothing written before OK, address and channel validation against everything but itself, Delete. |
| `test_stage_designer.py` | The designer's add and remove are incremental. |
| `test_clips.py` | The group structure, refs (uniqueness, round trip, the scene fallback across a reorder, `None` for the unresolvable). |
| `test_clip_library_ui.py` | The clip library's two routes into the fixture editor. |

---

## 12. Known limits and quirks

- **A scene's index is its MIDI address.** Adding, deleting or reordering scenes changes what saved `.mid` files fire
  (§6.2); favourites survive it, clips do not.
- **Chase timing is modelled but not played** (§6.3).
- **A type patched twice is one section of the clip library.** Its cards and favourites fire at the first instance by
  DMX address; the others are reached through the timeline.
- **The colour model is additive**, CMY included (§5).
- **Stale comments.** `ui/clip_library.py`'s `preview_requested` comment says the MIDI channel is the fixture's "patch
  index" (it is the patch entry's stable `channel`); `ui/fixture/__init__.py` announces "four surfaces" and does not
  list the Chases or Palette tabs.
