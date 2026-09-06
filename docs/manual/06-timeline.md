# 6. The timeline

The timeline is where a song's lighting is written. It shows the song's audio as
a waveform, its arrangement as labelled sections, and its lighting as *clips* on
one lane per patched fixture.

![The timeline at song scale: waveform with arrangement sections on top, one lane per fixture below.](images/timeline-clip-selected.png)

## 6.1 The transport bar

![The transport bar: edit controls on the left, transport in the middle, view controls on the right.](images/timeline-toolbar.png)

It sits directly above the tracks, and moves to the bottom edge when the stage is
undocked.

### 6.1.1 Left — edit controls

| Control | What it does |
|---|---|
| **+ Add track** | Patches a new fixture. Opens the stage designer and the fixture browser together — a lane *is* a fixture |
| **Snap** | Snapping on/off. **On by default** |
| **1 beat / 1/4 beat / 1/8 beat** | How fine the grid is. `1/4 beat` by default |
| **Bars \| Time** | What the ruler counts. Bars by default |
| **Markers** | Opens the marker-track editor — [6.1.4](#614-voice-tts-and-control-markers) |

### 6.1.2 Centre — transport

Stop, play/pause, and the position read-out (`00:00.000`).

The **♪** glyph beside the BPM is decorative — **the metronome is muted from the
CLICK row** in the header column, not here.

**BPM** accepts 40–300. Anything outside that range is refused with a dialog
naming the limits. The timeline can also detect a tempo from the audio.

### 6.1.3 Count-in

The **Count-in** switch is **per song**. On, the song begins with a counted-in
bar or two; off, it segues straight out of the previous song. How many bars — and
whether the last one is spoken aloud — is set once for the machine in
**Settings ▸ Audio** ([9.3](#93-audio)).

A count-in is *negative timeline time*: playback starts before zero and the
audio, MIDI, lights and metronome all begin together at t = 0. The position
read-out shows `COUNT-IN −0X.X` while it runs. Pausing during a count-in resets
to zero; seeking cancels it.

### 6.1.4 Voice TTS and control markers

![The marker track: voice TTS, tempo changes, stop-click and the segue.](images/voice-cues.png)

**Markers** opens the current song's *marker track* — everything that happens at a
point in the song without being a note. The strip along the top shows them laid
out over the song; drag one to move it. The table lists them in time order, and
the panel on the right edits whichever is selected.

Four kinds:

| Marker | What it does |
| --- | --- |
| **Voice TTS** | Speaks a line on the **VOICE** bus — in split routing the same side as the click, so it reaches the band and not the room. **Preview** renders the selected line and plays it. |
| **Tempo change** | Retimes the click from that point on, and re-draws the bar grid to match. The song's *base* tempo — what the `.mid` stores — is not touched. |
| **Stop click** | The click stops there and stays off for the rest of the song. Seeking back before the marker brings it back. |
| **Segue** | At the given time the show hands over to the next song in the setlist and starts it **with no count-in**. One per song, set with the tick box along the bottom. |

The **Time / Bar.beat** switch changes the units every time in the dialog is
shown and entered in. Bar.beat is read through the song's tempo map, so a
position after a tempo change converts differently from one before it.

Markers appear on the ruler in their own lane above the bar numbers, each a
flag coloured by kind — teal for voice TTS, violet for a tempo change, red for
the stop-click, amber for the segue — carrying its own name. **Drag a flag to
move the marker**; while you drag it, a dashed line runs down the arrangement
so you can see what you are landing on. **Double-click one** to open this
dialog on that marker.

Voice TTS needs a TTS voice installed (**Settings ▸ Audio**). Without one the
dialog warns *"TTS voice not ready (Settings ▸ AUDIO) — voice TTS stays
silent."* and nothing is spoken. The other three kinds do not use TTS and work
regardless.

### 6.1.5 BACKING | GUIDE

A song may carry a *guide* track — a full mix used as a design reference. This
switch chooses which of the two you are monitoring and which waveform is drawn.

> The monitor is **session-only**. It is never saved, always starts on BACKING,
> and **never affects what a live surface plays**. MIDI, DMX, the metronome and
> the markers are driven by the timeline and are identical under either.

The **GUIDE** button stays disabled until the current song actually has a guide
file.

### 6.1.6 Right — view controls

| Control | What it does |
|---|---|
| **Rows S \| M \| L** | **S collapses every lane** (it is not a height). M = 50 px, L = 80 px, and both resize the timeline section to fit |
| Zoom slider | 20 % – 400 %. Mirrors Ctrl+wheel and **Fit** |
| **Fit** | Zooms so the whole song fits the width |

## 6.2 What a clip is

This is the one idea the whole chapter rests on.

> A clip carries a MIDI **note**, a MIDI **channel** and a **velocity**.
> The **note is a verb** — set a channel, load a scene, run a chase, black out.
> The **channel selects which fixture** the verb acts on.
> The **velocity is that verb's parameter**.

So a clip is always edited *against the fixture whose lane it lives on*, never
against a general table of notes. The same note number means different things on
different fixtures, and the app resolves it from the patch every time.

Verbs are grouped by number:

| Notes | Verb |
|---|---|
| 1 – 23 | Set channel *n* of this fixture (DMX value = velocity × 2) |
| 24 – 35 | Load scene 0–11 of the fixture's current bank (velocity = blend time) |
| 36 – 47 | Select bank 0–11 |
| 48 / 49 / 50 | Take manual control / manual / black out this fixture |
| 51 | Black out everything (a toggle) |
| 52 – 59 | Timing, palette and scene-step verbs |
| 60 | **START** — initialise the palette |
| 61 – 69 | Song parts (INTRO, VERSE, CHORUS…) |
| 70 | Run a chase |
| 72 – 75 | Colour macros |

**Only a fixture's first 23 DMX slots can be driven from the timeline**, because
notes 24 and up are already scene verbs. Channels past that are still patched,
still rendered, and still reachable from the fixture's stored scenes — they just
cannot be authored clip by clip.

## 6.3 Anatomy

### The header column

![The header column: the three output buses over one header per fixture lane.](images/timeline-track-header.png)

At the top, **TRACKS** with a live count, then the three output buses. Each is a
label, an **M** mute button, a level slider and a decibel read-out (`0dB` at
full, `-∞` at zero):

- **MASTER** — the backing track
- **CLICK** — the metronome
- **VOICE** — the spoken count-in and voice TTS

Below them, one header per fixture:

| Element | What it does |
|---|---|
| grip | Drag to reorder the lanes. The order is remembered with the show |
| chevron | Collapses the lane to a single row. View only |
| colour swatch | Matches this fixture's group in the clip library and its section in the DMX monitor |
| name / `Ch. 0 · DMX 1–7` | The fixture's name over its MIDI channel and address range |
| **eye** | Mutes this lane's MIDI output. Its clips redraw dimmed |
| **lock** | Makes the lane read-only: no moves, no resizes, no new clips, no editing |
| **A** | Follow the song-part program automatically |

**Double-click the header** — anywhere on it that isn't one of those controls —
to open the **Edit fixture** dialog for that lane's fixture
([5.6](#56-editing-a-fixture-you-have-already-patched)):
its name, mode, address and MIDI channel, and its type's scenes and chases.
Right-clicking the header offers the same thing as a menu item. A lane *is* one
patched fixture, so there is never any doubt which one you get.

**The header says so as you point at it**: the fixture's colour stripe down the
left edge lightens, and the address line underneath the name turns into
*"Double-click to edit"*, then goes back to the address when you move away.

### The lanes

![Zoomed in to a few bars: the ruler in Bars mode, the waveform with its arrangement, and clips on the fixture lanes.](images/timeline-full.png)

The ruler runs along the top in **Bars** or **Time**. Beneath it the waveform,
overlaid with the song's arrangement as translucent labelled boxes (INTRO,
VERSE, CHORUS…). Then the fixture lanes, ordered by DMX start address. A red
playhead crosses the lot.

Two kinds of block appear on a lane, and telling them apart matters:

- **Clips** — solid, rounded, coloured by the verb. These are yours: click,
  drag, resize, edit, delete.
- **Auto blocks** — square-cornered, diagonally hatched, dashed border. These
  appear only on lanes whose **A** button is on. They are a *preview of the
  automatic song-part program*, not editable and not saved.

A clip's colour comes from its verb; a scene group's is mixed from its channel
values, so a warm look is a warm block. Muted lanes draw everything at about a
third opacity.

### BLACKOUTs are the gaps

Between clips, a lane is not empty — it holds invisible **BLACKOUT** markers.
They are never drawn and cannot be clicked. They exist so a clip's extent is
explicit: a gap is a decision, not an absence.

This has one consequence you will meet within the first hour:

> **`Delete Event` does not delete a clip. It replaces it with a blackout.**
> That is why neighbouring clips do not expand to fill the hole. To actually
> remove the marker afterwards, right-click it and choose **Remove blackout**.

## 6.4 Undo and redo

**Ctrl + Z** and **Ctrl + Shift + Z**. Every committed edit is undoable, and
multi-step edits collapse into a single step.

There is no undo button, no menu entry, and nothing greys out when the history is
empty — pressing Ctrl+Z with nothing to undo simply does nothing.

## 6.5 Selecting

| Action | Result |
|---|---|
| Click a clip | Selects just that one |
| **Ctrl + click** a clip | Adds it to / removes it from the selection |
| Drag on empty lane space | Rubber-band marquee; everything it touches is selected |
| **Ctrl + drag** on empty space | Same, but keeps the existing selection |
| Click empty space (no drag) | **Seeks the playhead** to that point |

Selected clips get a warm gold border.

![Five clips selected at once: in multi-selection the whole clip body becomes a move handle.](images/timeline-multi-selection.png)

With two or more clips selected the timeline enters *multi mode*: fade handles
disappear, edge-resizing is switched off, the whole body of every clip becomes a
move handle, and dragging any one of them slides the entire selection as a rigid
block.

Selection survives redraws — it is tracked by clip, not by screen position.

## 6.6 Moving, resizing and fading

Grab a clip in the middle to **move** it. Grab within about 8 pixels of either
edge to **resize** from that side; the cursor changes to tell you which you are
about to get. On a clip too narrow to have a middle, the left half resizes left
and the right half resizes right.

- **Nothing is written until you let go.** Everything during the drag is a
  preview.
- **Clips never overlap.** A move is clamped by its neighbours, and can hop over
  one when there is a big enough slot on the far side.
- **Snapping** pulls the edge you are dragging to nearby clip edges and to the
  finest visible grid line — bars and their subdivisions in Bars mode, seconds in
  Time mode. Turn it off with **Snap** on the toolbar.

**Fades** are the two small gold triangles on a clip's top edge: the left one is
where the fade-in ends, the right one where the fade-out begins. Drag them along
the clip. There are no cross-fades — each handle treats the other as a wall — and
shortening a clip shrinks any fade that would have fallen off it. Fade handles
are not offered on blackouts, on locked lanes, or while several clips are
selected.

## 6.7 Adding clips

Three ways, all equivalent:

1. **Drag from the clip library** (chapter [8](#8-the-clip-library-and-the-dmx-monitor)).
   The drop is validated live: over a lane where the verb has no meaning, over a
   locked lane, or over a fixture whose type does not match the scene you are
   dragging, the cursor shows *rejected*. **Song parts are the exception — they
   drop onto the waveform band**, not onto a fixture lane, because they belong to
   the song rather than to a light.
2. **Right-click a lane** and choose **Add event at this position**.
3. **Ctrl + V** pastes a copied selection at the playhead.

## 6.8 Context menus

Right-clicking a clip gives, in order: **Edit Properties…** (omitted on a
blackout), **Add event at this position**, and **Delete Event** — or **Remove
blackout** on a blackout. With several clips selected, the menu collapses to a
single **Delete N events**.

Right-clicking empty lane space offers only **Add event at this position**.

Right-clicking a song-section box offers **Edit '<part>'…** and **Delete
'<part>'**.

Right-clicking a lane *header* offers **Edit '<fixture>'…**, which opens that
fixture's own dialog ([5.6](#56-editing-a-fixture-you-have-already-patched)).

Hovering a clip shows its part name, note number, time, bar and beat, and
velocity.

## 6.9 The clip editor

Double-click a clip, or use **Edit Properties…**. Which editor opens depends on
what the clip is.

### A single verb

![Editing one clip: the verb picker, that verb's own parameter control, and the time field.](images/clip-edit-dialog.png)

The header names the fixture, its mode and its placement (`ch 1 · DMX 33-40`).

**EVENT TYPE** lists every verb that is meaningful *on this fixture* — the
picker is gated by exactly the rule drag-and-drop uses, so the dialog can never
author a clip a drop would have refused. Below it sits **that verb's own
control**, because a chase index and a blend time are not the same thing:

| Verb | Control |
|---|---|
| Set a channel | A console fader, coloured by channel family, captioned live with what the capability under that value means (`200 of 255 — ColorIntensity`, `· 4.2 Hz`, `· 45°`) |
| Load a scene | The scene it targets, plus a **blend** time |
| Bank + scene in one | A **bank** and a **scene** drop-down |
| Run a chase | The chase, by name |
| Blend / colour / chase timing | A seconds spin, 0.0 – 12.7 |
| Blackout, take manual, … | No parameter — just a line explaining what it does |

> **Retyping a clip resets its velocity.** A channel-at-200 clip has velocity
> 100; carried across to "run a chase", that would select chase number 100.
> The editor deliberately starts the new verb from its own default.

**TIME** re-times the clip. It goes through the same search as a drag, so it can
refuse when there is no room — the clip then simply stays where it was.

### A scene group

![Editing a scene: the console, the colour wheel and one fader per channel.](images/clip-edit-scene.png)

A *scene clip* is seven simultaneous events sharing an identity, drawn as one
block. Editing it opens the full console: the scene's name, **Load from library
▾**, **Save as scene…**, the **SIMULATED OUTPUT** card, a **COLOUR** wheel
(offered only when the fixture actually mixes a colour) and the fader strip.

**Load from library** copies a stored scene's values into this clip. The clip
stays a *manual* scene and will not follow later library edits — if you want one
that does, use a Scene verb instead.

**The fader strip** works like the one in the fixture browser: press anywhere to
jump, drag to follow, right-click for full and again for zero, **▾** on a channel
name to pick from that channel's own options.

Two behaviours are specific to scene editing:

- **Channels the scene does not carry are drawn dimmed.** Move one and it joins
  the scene. This is not cosmetic: at runtime an absent channel keeps whatever
  the previous frame left in it, while a present channel at its default *forces*
  that value. Submitting every fader would silently widen the scene.
- **Channels past DMX slot 23 are greyed out**, for the reason in
  [6.2](#62-what-a-clip-is).

> **"Save as scene…" writes to the fixture library immediately.** It is not part
> of OK, and Cancel will not undo it — it edits a fixture *type*, which every
> show using that type shares. If the name already exists, the app offers to
> overwrite in place, and recommends it: a saved song binds a scene by position,
> so adding a second scene with the same name shifts everything after it.

### Live output

The **Live output** checkbox sends what you are dialling to the rig while you
edit.

- It is **never on by default and never remembered** — this dialog gets opened in
  front of an audience, and a fader drag would be visible.
- It is **disabled while the transport is playing** (the sequencer would fight
  it for the same channels) and on a locked lane.
- Payloads lead with a *take manual* instruction, or the automatic program would
  overwrite the preview on the next frame.
- **Cancel re-sends what the clip held when the dialog opened.** That is a
  re-send, not a restore: it cannot put back what the automatic program was
  doing.

### The START clip

![The START clip's own editor: a base hue and the palette mode built from it.](images/start-event-edit.png)

Note 60 gets its own dialog, because its velocity packs two choices rather than
one parameter: a **Base Color** from a twelve-hue grid (or **?** for random) and
a **Palette Mode** — COMPLEMENTARY, ANALOGOUS, TRIADIC, COLOR+WHITE,
MONOCHROMATIC or SINGLE — each previewed as a live colour band.
