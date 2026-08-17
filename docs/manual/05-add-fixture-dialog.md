# 5. The Add fixture dialog

Reached from **Edit stage ▸ Add fixture**, or straight from the timeline with
**+ Add track** (a timeline lane *is* a patched fixture, so both open the same
flow).

![The fixture browser: library on the left, the selected fixture's detail tabs in the middle, and the patch decision along the bottom.](images/add-fixture-library.png)

The dialog has three regions, and they answer three different questions:

1. **the library**, on the left — *which fixture?*
2. **the detail tabs**, in the middle — *is this really the one?*
3. **the footer**, along the bottom — *where does it go?*

Only the footer changes your show. Everything else is a browser.

## 5.1 The library

The fixture library is the [Open Fixture Library](https://open-fixture-library.org/)
format, so any OFL definition works. Fixtures come from three layers, each
overriding the one before: the definitions shipped with the application, the
ones you have added under your preferences folder, and any that travel inside the
open show.

**Search** (`Search fixtures, manufacturers…`) matches across manufacturers and
fixture names, and the counter beside it shows how much of the library you are
looking at — `155 / 634`. Groups start collapsed and expand automatically while
you are searching.

The **Manufacturer | Category** segmented control changes how the tree is
grouped. Whichever grouping you are *not* using appears as a dim caption on each
row, so a manufacturer-grouped list still tells you that *Spica 250M* is a Moving
Head. Fixtures with no manufacturer folder collect under **Generic**;
uncategorised ones under **Uncategorised**.

## 5.2 The detail tabs

At the top of the panel: the manufacturer, the fixture name, its category chips,
and three headline numbers — **CHANNELS**, **MODES**, **POWER**.

### General

Up to four cards, each hidden when the definition has nothing to put in it:

- **PHYSICAL** — dimensions, weight, power draw, power and DMX connectors, bulb,
  lens.
- **METADATA** — short name, authors, created and last-modified dates, the
  import plugin the definition came through, and RDM support.
- **LINKS** — buttons that open the manufacturer's manual, product page or video
  in your system browser.
- **COMMENT** — whatever notes the definition carries.

### Channels

![The Channels tab: every DMX slot of the selected mode, addressed from the chosen start.](images/add-fixture-channels.png)

Every DMX slot of the currently selected mode, **addressed from the DMX start
you have picked in the footer** — so the `ADDRESS` column reads `01 · 94`, the
slot within the fixture and the real universe address side by side. The caption
above the table names the mode, and the range appears on the right.

Each row carries the channel name and a colour-coded type chip (`Pan`, `Tilt`,
`Intensity`, `Color`, `Gobo`, `Shutter`, `Maintenance`…). The **▸** on the right
expands the row into that channel's capability ranges — the low and high DMX
values, what the manufacturer calls each one, and its capability type.

A slot the parser could not resolve is shown as **—** and kept in place. It still
consumes an address, which is what keeps the rest of the layout honest.

### Modes

![The Modes tab: one card per mode, with its footprint and its channel list.](images/add-fixture-modes.png)

One card per mode: a radio button, the mode name, its short-name chip, its
footprint (`16 ch · 94-109`), and a flow of every channel name in it. This is
where you choose which mode to patch. The **Channels** tab follows your choice.

**Pick the mode the fixture is actually set to.** A moving head in its 8-channel
mode patched here as 16-channel will respond to the wrong addresses for
everything after the first few.

### Scenes

![The Scenes tab: banks and scenes for this fixture type, with the console that dials them in.](images/add-fixture-scenes.png)

This tab edits stored looks for the fixture **type** — see
[5.4](#54-the-scenes-tab).

## 5.3 The footer — the patch decision

![The footer refusing an illegal patch: the address overlaps an existing fixture, so OK is disabled.](images/add-fixture-footer.png)

| Control | What it tells you |
|---|---|
| **DMX START** | The address, 1–512. Beside it, `occupies 94 – 109` and the status: `free · no overlap`, `overlaps Ledbar 1`, or `needs 16 ch · past 512` |
| **MIDI CHANNEL** | The channel, 0–15, with all sixteen as clickable buttons. Channels already in use are marked, and picking one says `already used` |
| **PatchBar** | The whole universe as a strip. Hatched blocks are already patched, the solid blue block is where this fixture would land — **and it turns red when it collides** |
| **UNIVERSE 0 · PATCH n / 512 USED** | How much of the universe the show is using |
| **✓ OK** / **⊘ Cancel** | **OK stays disabled until the patch is legal** |

You cannot create an illegal patch from this dialog. The Inspector applies the
same two checks afterwards, so you cannot edit your way into one either.

## 5.4 The Scenes tab

A *scene* is a named set of channel values for a fixture type, stored in a
*bank*. Banks and scene numbers are what the MIDI verbs address, which is why
they are numbered rather than just named.

**Left panel.** A tree of banks with their scenes beneath. Each scene shows a
colour swatch painted by the real renderer and, if it has any, abbreviated
song-part tags. **+ New** offers **New scene** and **New bank…**; **Duplicate**
and **Delete** work on whatever is selected. Banks and scenes are renamed
in place by clicking the name, and reordered by dragging.

**Right panel.** The scene name, a row of nine song-part chips (INTRO, VERSE,
CHORUS, BREAK, BRIDGE, INST, SOLO, OUTRO, END — tag a scene and the app will
offer it automatically during that part of a song), the **SIMULATED OUTPUT**
card, and the channel faders.

**SIMULATED OUTPUT** is the stage's own renderer fed the values you are dialling,
next to a read-out of what they resolve to — intensity, colour, pan and tilt,
strobe, effect, zoom. It is not an approximation of the stage; it *is* the stage.

**CHANNEL FADERS** is a lighting console. Press anywhere on a fader and the value
jumps there, then follows the drag. **Right-click flashes a channel to full;
right-click again returns it to zero.** A channel whose capabilities name
discrete options gets a **▾** beside its name — click it and those options appear
as a grid of live tiles (animated, when the definition ships artwork for them).

**The workflow** is: dial a look → type a name → **Create**. Selecting an
existing scene loads it and the button becomes **Save**. The status line under
the tree always says where you stand — *"New scene — Create adds it to
SCENES."*, *"Unsaved changes — press Save to write them to the fixture's
.tdmx.json."*, or a count of what is stored.

> **Two things about this tab are worth knowing before you use it.**
>
> **It is not covered by OK and Cancel.** This tab edits the fixture *library*,
> which every show using that type shares. **Create** and **Save** write to disk
> immediately. Cancelling the dialog does not undo them. (If the definition sits
> somewhere the app cannot write — inside the application bundle — it is forked
> into your own fixtures folder first, and your copy wins from then on.)
>
> **A saved song binds a scene by its position, not its name.** MIDI verbs
> address "scene 3 of bank 1". Renaming a scene or a bank is safe. **Inserting,
> deleting or reordering scenes shifts the numbering, and existing songs will
> load a different look.** Add new scenes at the end of a bank, or in a new bank,
> once a show depends on them.
