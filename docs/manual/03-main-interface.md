# 3. The main interface

The window has four zones and no menu bar. Everything lives on the top toolbar,
on the timeline's own toolbar, or behind the **☰** button in the top-right
corner.

![The four zones: the show toolbar across the top, the virtual stage, the right sidebar, and the timeline along the bottom.](images/main-window.png)

| Zone | What it is | Chapter |
|---|---|---|
| **Top toolbar** | The show: project, setlist, song files, version history | [7](#7-the-top-toolbar) |
| **Virtual stage** | The rig, drawn live from the DMX being sent | [4](#4-the-stage-view) |
| **Right sidebar** | Two tabs — **Library** (draggable cues) and **DMX Channels** | [8](#8-the-clip-library-and-the-dmx-monitor) |
| **Timeline** | Transport bar over one lane per patched fixture | [6](#6-the-timeline) |

## 3.1 The window title

The title bar composes what is currently open:

```
TupperDMX Simulator — Billares2026.show - 10-supersubmarina.mp3
```

The show name appears once a project is open, the file name once the current
song has a backing track. If you are ever unsure which project you are editing,
this is the fastest place to look.

## 3.2 Resizing the zones

Two drag handles divide the window, and both remember where you left them.

- **Between the stage and the timeline** — a horizontal divider. Drag it up to
  give the timeline more lanes, down to make the stage bigger. Neither pane can
  be collapsed to nothing.
- **Between the stage and the sidebar** — a vertical divider.

The sidebar also has a one-click hide: the small **chevron** at the top-right
corner of the stage slides it out of the way over about a fifth of a second and
gives the whole width to the stage. The button's tooltip flips between **"Hide
DMX panel"** and **"Show DMX panel"**, and the state is remembered between
sessions.

## 3.3 The stage overlay buttons

Three buttons float over the stage picture rather than sitting in a toolbar, so
they do not steal room from the rig.

| Button | Position | What it does |
|---|---|---|
| **Edit stage** (pencil) | top-right of the stage image | Enters the stage designer — grid on, fixtures become draggable |
| **Undock** (two rectangles) | left of *Edit stage* | Moves the stage into its own **Virtual Stage** window, typically for a second display |
| **Chevron** | top-right of the viewport | Hides or shows the right sidebar |

Once the stage is undocked, the timeline takes over the whole main window and
its toolbar moves to the bottom edge. The stage window can be closed to dock it
back; if it ends up on a display you no longer have, **☰ ▸ Dock stage** brings it
home.

**F** puts the stage alone on a whole screen, and **F** or **Esc** brings it back
to where it was — docked or floating ([4.6](#46-the-stage-in-its-own-window)).

## 3.4 The position display

The transport's read-out, in the middle of the timeline toolbar, shows
`MM:SS.mmm` while the song runs.

During a count-in it counts *down*, with a minus sign:

```
−0:03.200
```

This is not a quirk of the display. **A count-in is negative timeline time.**
Playback starts before zero, the playhead waits at the left edge, and audio,
MIDI, lights and the metronome all begin at t = 0 together. Pausing during the
count-in resets to zero; seeking cancels it. See [6.1.3](#613-count-in).

## 3.5 Keyboard shortcuts

| Key | Where | Action |
|---|---|---|
| **Space** | anywhere | Play / pause |
| **←** / **→** | anywhere | Move the playhead. A tap steps half a second; holding the key speeds up to five seconds a step |
| **F** | anywhere | The stage fullscreen, and back |
| **Esc** | stage | Back out one level: close the Inspector, then leave the stage designer, then leave fullscreen |
| **Ctrl + Z** | anywhere | Undo the last timeline edit |
| **Ctrl + Shift + Z** | anywhere | Redo |
| **Delete** | timeline | Replace the selected clips with blackouts; selected song sections are removed |
| **Alt + ←** / **Alt + →** | timeline | Nudge the selected song sections a bar (with **Shift**, a beat) — [6.10](#610-song-sections) |
| **Ctrl + C** / **Ctrl + V** | timeline | Copy the selection / paste it at the playhead |
| **Ctrl + mouse wheel** | timeline | Zoom horizontally, anchored under the pointer |

**Space**, the arrows and **F** are deliberately suppressed while a text field,
a spin box or an editable drop-down has focus, so typing a song title never
starts the show. They are also suppressed while a dialog is open. The arrows
additionally leave a slider, a list or a tab bar alone, since those steer with
them.

Undo and redo have **no button and no menu entry**, and nothing greys out when
there is nothing left to undo — the shortcuts are the whole interface. See
[6.4](#64-undo-and-redo).

## 3.6 The ☰ menu

The hamburger button at the far right of the top toolbar holds what does not
belong to any one section:

| Entry | Notes |
|---|---|
| **Auto program…** | Generates a song's light show from its audio — chapter [11](#11-the-auto-program) |
| **Snapshot** / **Restore version ▸** | The show's version history — [7.4](#74-the-menu-history-and-the-rest) |
| **Dock stage** | Disabled unless the stage is floating |
| **Settings…** | Opens the preferences dialog — chapter [9](#9-settings) |
| **About TupperDMX** | Version and build provenance |

On a narrow window the SHOW card's actions fold into the top of this menu too.
