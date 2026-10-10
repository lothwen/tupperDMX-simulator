# Appendix A. MIDI note reference

Every clip in this application is a MIDI note, and the same three-part rule
decides what it does:

> The **note number** is a *verb*.
> The **MIDI channel** selects *which fixture* the verb acts on.
> The **velocity** is that verb's *parameter*.

You do not need this table to use the timeline — the clip editor names every
verb in words and gives each one its own control. It is here for the cases where
you are looking at raw MIDI: authoring a show in another sequencer, driving the
lighting from BBeat in [mode 1](#21-mode-1-bbeat-and-the-arduino-controller), or
working out why a note imported from somewhere else does nothing.

## A.1 The full layout

*Scope* says whether the MIDI channel matters. **Global** verbs are dispatched
before the fixture is looked up, so they act on the whole rig whatever channel
they arrive on. **Fixture** verbs do nothing at all if no patched fixture owns
that channel.

| Note | Verb | Scope | Velocity means |
|---|---|---|---|
| 0 | *reserved* | — | — |
| **1 – 23** | Set DMX channel *n* of this fixture | fixture | **DMX value = velocity × 2** |
| **24 – 35** | Load scene *n* − 24 of the fixture's **current** bank | fixture | blend time in units of 100 ms |
| **36 – 47** | Select bank *n* − 36 | fixture | ignored |
| 48 | Take manual control | fixture | ignored |
| 49 | Manual — hold the current frame | fixture | ignored |
| 50 | Black out this fixture | fixture | ignored |
| **51** | **Black out everything** — a toggle | **global** | ignored |
| 52 | Blend time | fixture | milliseconds = velocity × 100 |
| 53 | Colour-change time | fixture | milliseconds = velocity × 100 |
| 54 | Chase-change time | fixture | milliseconds = velocity × 100 |
| 55 | Palette random **on** | fixture | ignored |
| 56 | Palette random **off** | fixture | ignored |
| 57 | Previous scene in the current bank | fixture | blend time × 100 ms |
| 58 | Next scene in the current bank | fixture | blend time × 100 ms |
| 59 | Load bank **and** scene together | fixture | **bank = velocity ÷ 8, scene = velocity mod 8** |
| 60 | **START** — the fixture's colour palette; never produces light on its own | fixture | **hue = velocity ÷ 6, mode = velocity mod 6** |
| **61 – 69** | Song part — INTRO … END | **global** | ignored |
| 70 | Load chase | fixture | chase index |
| 71 | Track setup flags — never produces light | fixture | bit 0 auto, bit 1 mute, bit 2 lock |
| **72 – 75** | Colour macro — WHITE / ORANGE / CYAN / PINK | fixture | ignored |
| 76 – 127 | *reserved* | — | — |

## A.2 Song parts (notes 61 – 69)

A song-part marker does not set a look directly. It tells every fixture that is
in **auto** mode to load whatever scene or chase its own definition has tagged
for that part; fixtures with nothing tagged for it are blacked out. That is why
the same marker produces a different look on every fixture, and why tagging
scenes in the fixture Scenes tab ([5.4](#54-the-scenes-tab)) is what makes the
automatic program work at all.

| Note | Part | Note | Part | Note | Part |
|---|---|---|---|---|---|
| 61 | INTRO | 64 | BREAK | 67 | SOLO |
| 62 | VERSE | 65 | BRIDGE | 68 | OUTRO |
| 63 | CHORUS | 66 | INST | 69 | END |

These are the markers you drag onto the **waveform**, not onto a fixture lane —
they belong to the song rather than to a light.

## A.3 Colour macros (notes 72 – 75)

| Note | Macro | RGB |
|---|---|---|
| 72 | WHITE | `255, 255, 230` |
| 73 | ORANGE | `255, 165, 0` |
| 74 | CYAN | `0, 255, 255` |
| 75 | PINK | `255, 192, 203` |

The colour is fitted to whatever the target fixture can actually mix, so a macro
sent to a fixture with no colour channels does nothing rather than something
wrong.

## A.4 The START palette (note 60)

Note 60 packs two choices into one velocity:

```
hue_index    = velocity ÷ 6      # 0–11 pick a fixed hue; 12 and up = random
palette_mode = velocity mod 6    # see the table below
```

| Value | Palette mode | What it builds |
|---|---|---|
| 0 | COMPLEMENTARY | the hue and its opposite |
| 1 | ANALOGOUS | the hue and its two neighbours |
| 2 | TRIADIC | three hues evenly spaced around the wheel |
| 3 | COLOR+WHITE | the hue alternating with white |
| 4 | MONOCHROMATIC | one hue at several lightnesses |
| 5 | SINGLE | the hue alone |

So velocity 14 is hue 2 in TRIADIC mode; velocity 75 is a random hue in
MONOCHROMATIC. The **Palette** tab of the fixture dialog
([5.6](#56-the-palette-tab)) is a picker over exactly this arithmetic.

Setting a palette does **not** start it cycling. It stores the palette on the
fixture; a song-part marker is what arms it.

### Note 60 and note 71 are state, not cues

They are the two notes that are never clips. Each song's `.mid` holds **at most
one of each per fixture**, pinned at t=0 and living outside the timeline: they
persist and they replay, but nothing draws them and nothing can drop one. The
`.mid` therefore stores no note 60 for a fixture on the default palette (hue 0,
COMPLEMENTARY) or a fixture with all three setup flags off — velocity 0 is a
note-off, so **absence is how the default is written**.

## A.5 Things that catch people out

**A velocity of 0 is a note-off.** That is MIDI, not a choice this application
made. A verb whose parameter is genuinely zero — a channel at 0, a blend of
0 ms — cannot be expressed as a note-on with velocity 0. Scene loads work around
it by clamping the blend to at least 100 ms; for a channel, send the *blackout*
verb (note 50) rather than a zero.

**Channel values are doubled, so only even values are reachable.** Velocity 100
means DMX 200. An odd DMX value cannot be sent from a clip; the clip editor says
so when you try, and shows what will actually be replayed.

**Only a fixture's first 23 DMX slots can be driven this way**, because note 24
is already a scene verb. Channels past that are still patched, still rendered
and still reachable from the fixture's stored scenes — they just cannot be
addressed clip by clip. → [6.2](#62-what-a-clip-is)

**Notes 24 – 35 depend on the current bank.** They load a scene *by position in
whichever bank the fixture is currently on*, so a bank-select verb (36 – 47)
earlier on the lane decides what they mean. Note 59 avoids the ordering problem
by carrying both, at the cost of reaching only the first eight scenes of a bank.

**Scene numbering is positional.** Inserting, deleting or reordering scenes in a
bank shifts every note after it, and a saved song will load a different look.
Renaming is always safe. → [5.4](#54-the-scenes-tab)

**Note 71 never makes light.** It carries a lane's own settings — auto, mute,
lock — so they survive in the `.mid`. The runtime ignores it and the timeline
does not draw it as a clip.

## A.6 One universal layout, many fixtures

Nothing above names a fixture type. A verb resolves against whatever definition
the addressed fixture has: "channel 3" is that fixture's third DMX slot,
"scene 2" is the second scene of its current bank, "chase 4" its fifth chase. The
one layout works for a six-channel PAR and a thirty-two-channel moving head
without a special case for either.

That is also what makes [mode 1](#21-mode-1-bbeat-and-the-arduino-controller)
work: the hardware controller reads the same fixture definitions and dispatches
the same notes, so a show designed against the simulator plays back identically
on the real rig.
