# 11. A show from start to finish

This chapter is one continuous worked example: an empty project on a Monday, a
gig on a Saturday. Each step points at the chapter that covers it properly.

## Step 1 — Create the show

**SHOW ▸ + New**. Name it after the gig or the tour (`Billares2026`), and choose
where it lives; `Documents/TupperDMX/` is the default and is fine.

You now have a folder. Everything from here — the setlist, the rig, the cues —
goes inside it. → [6.1](#61-the-transport-bar)

## Step 2 — Load the first backing track

**SONG FILES ▸ BACKING TRACK ▸ + Load backing**, and pick the MP3.

The song appears in the setlist, named after the file. Rename it by typing over
the title in the **SETLIST** selector and pressing Enter.

Repeat for the rest of the set. Songs are added in the order you load them.
→ [6.2](#62-what-a-clip-is)

## Step 3 — Get the tempo and the offset right

Set the **BPM** on the transport bar, or let the timeline detect it from the
audio. Then check the grid against the waveform: if the bar lines drift off the
downbeats, the tempo is close but wrong.

If the bar lines are *parallel* to the beats but shifted, the problem is the
offset — **drag the waveform horizontally** to slide the audio against the grid
until bar 1 lands on beat 1.

Get this right before writing any lighting. Every clip you place is positioned
against this grid. → [6.1.2](#612-centre-transport)

## Step 4 — Patch the rig

**Edit stage ▸ Add fixture**, once per light.

For each one: find it in the library (search by manufacturer or model), check the
**Modes** tab and pick the mode your fixture is actually set to, then set the
**DMX START** and the **MIDI CHANNEL** in the footer. Watch the patch bar — the
block turns red if the address collides, and **OK** stays disabled until the
patch is legal.

Then drag the fixture to where it stands on the real stage. Drop it high for a
flown fixture, low for one on the deck; correct the guess in the Inspector's
**MOUNTING** switch if it got it wrong.

**Set the MIDI channel once and leave it.** Every clip you write from now on is
bound to its fixture by that channel. → [5](#5-the-add-fixture-dialog),
[4.5](#45-the-inspector)

![The rig patched and placed, with the Inspector open on the moving head.](images/stage-designer.png)

Raise the **HAZE** slider to something like 50–60 %. Without haze in the air a
beam is invisible, and you will spend an hour wondering why the moving head looks
broken. → [4.3](#43-the-edit-toolbar)

Click **Done**.

## Step 5 — Build the looks you will reuse

Before writing any timeline, build the handful of looks the show keeps coming
back to — a warm wash, a cold wash, a blinder hit, a beam look.

Open **Add fixture** again and go to the **Scenes** tab (you can cancel out of
the dialog afterwards; this tab writes on its own). Dial a look on the faders,
watch it in **SIMULATED OUTPUT**, name it, press **Create**.

Tag scenes with song parts (CHORUS, VERSE…) if you want the app to be able to
offer them automatically.

> Remember that scenes belong to the fixture **type** and are shared by every
> show that uses it — and that a saved song addresses a scene by its position in
> the bank, so add new ones at the end. → [5.4](#54-the-scenes-tab)

## Step 6 — Write the lighting

Select a song, then start dropping cues from the **Library** onto the fixture
lanes.

- **Click a card first** to audition it. The stage shows you exactly what it does
  before it becomes a clip.
- **Drag it onto the lane** of the fixture it belongs to. The cursor tells you
  when a drop makes no sense on that lane.
- Drag **song parts** onto the *waveform*, not onto a lane — they mark the
  arrangement.

Then shape what you dropped: drag clips to time, drag their edges to length, drag
the gold triangles for fades. Nothing is committed until you release the mouse,
and **Ctrl+Z** undoes anything you regret. → [6.7](#67-adding-clips),
[6.6](#66-moving-resizing-and-fading)

![Clips written across the fixture lanes, with the arrangement marked on the waveform.](images/timeline-clip-selected.png)

Double-click any clip to open its editor and set its parameter precisely — a
blend time, a chase, an exact channel value. → [6.9](#69-the-clip-editor)

> Two habits that save time later: **use the lock button** on lanes you have
> finished so a stray drag cannot disturb them, and remember that **deleting a
> clip leaves a blackout** in its place, which is usually what you want.

## Step 7 — Count-ins and cues for the band

Turn the **Count-in** switch on for songs that start cold, and off for songs that
segue. Set how many bars — and whether the last one is counted aloud — once, in
**Settings ▸ Audio**.

Add spoken cues with the **Cues** button for anything the band needs warning
about (*"entra batería"*, *"solo"*). They play on the voice bus, which in split
routing goes to the in-ears and not to the room.
→ [6.1.3](#613-count-in), [6.1.4](#614-voice-cues)

## Step 8 — Set up the audio for the stage

**Settings ▸ Audio**: pick the interface, and choose a **Split** routing so the
backing goes to the PA on one channel and the click plus voice go to the in-ears
on the other. Check the balance with the **MASTER**, **CLICK** and **VOICE**
sliders in the timeline's header column. → [9.3](#93-audio)

## Step 9 — Route the lights

At home, leave **Quick mode** on **All software**: the internal controller runs
and feeds the virtual stage, and you need nothing plugged in.

At the venue, switch to **Software + real lights** and fill in the Art-Net
address of your node (or tick **USB DMX** and pick the interface). The signal
flow panel on the right should read **✓ Routing valid — no conflicts**.

Check the **DMX Channels** tab: if the numbers are moving there, the application
is producing DMX and anything still dark is downstream. → [8.1](#81-the-library-tab),
[8.2](#82-the-dmx-channels-tab)

## Step 10 — Set up the tablet

**Settings ▸ Web Remote**: switch it on, note the address, open it on the tablet.

Check the sync pill says **SYNCED**, run a song, and confirm the setlist, the
section countdown and the cue buttons all do what you expect from the far side of
the stage. → [10](#10-the-web-remote)

## Step 11 — Rehearse, then pin a snapshot

Run the set. Fix what looks wrong.

When it is right, **VERSIONS ▸ Snapshot** and name it (`before Billares`). A
named snapshot is pinned and never pruned, so whatever happens during load-in you
have a known-good state one click away — and restoring it snapshots the current
state first, so even the rollback is reversible. → [6.4](#64-undo-and-redo)

## Step 12 — Take it to the gig

**SHOW ▸ Export** writes a single `.showpack` with the audio, the MIDI, the patch
and the fixture definitions for every song. Copy it to the machine that will run
the show and use **SHOW ▸ … ▸ Import bundle…**.

If the show runs from a rack box with no screen, start it with `--headless` and
drive the whole night from the tablet. → [6.1](#61-the-transport-bar),
[10.4](#104-rack-mode)

---

## Quick reference

| I want to… | Go to |
|---|---|
| Add a light | **Edit stage ▸ Add fixture**, or **+ Add track** on the timeline |
| Move a light on stage | **Edit stage**, then drag it |
| Change a light's address or channel | **Edit stage**, select it, use the Inspector |
| Make a beam visible | Raise **HAZE** on the edit toolbar |
| Save a look for reuse | **Add fixture ▸ Scenes** tab, or **Save as scene…** in a scene clip |
| Try a cue without committing | **Click** it in the Library |
| Add a cue to a song | **Drag** it from the Library onto the fixture's lane |
| Undo | **Ctrl + Z** |
| Really remove a clip | Delete it, then right-click the blackout ▸ **Remove blackout** |
| Send lights to a real rig | **Settings ▸ Input / Output ▸ Software + real lights** |
| Send the click to the in-ears only | **Settings ▸ Audio ▸ Routing ▸ Split** |
| Run the show from a tablet | **Settings ▸ Web Remote** |
| Go back to yesterday | **VERSIONS**, pick a snapshot, **Restore** |
| Move the show to another machine | **SHOW ▸ Export**, then **… ▸ Import bundle…** |
| Start over with settings | Delete `userprefs.json` — shows are not affected |
