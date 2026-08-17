# TupperDMX Simulator

[![Latest release](https://img.shields.io/github/v/release/lothwen/tupperDMX-simulator?label=release&color=blue)](https://github.com/lothwen/tupperDMX-simulator/releases/latest)
[![Build](https://github.com/lothwen/tupperDMX-simulator/actions/workflows/build.yml/badge.svg)](https://github.com/lothwen/tupperDMX-simulator/actions/workflows/build.yml)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue)](https://www.python.org/)
[![License: WTFPL](https://img.shields.io/badge/license-WTFPL-lightgrey)](LICENSE.txt)

A lighting-control application for live bands. It plays the backing track, runs the
click for the drummer, fires DMX cues from a MIDI timeline and draws the result on a
virtual stage — all from one clock, so the lights land on the beat whether or not any
lighting hardware is plugged in.

Design the show at home against a virtual rig, carry the whole project to the venue in
one folder, run the night from a tablet.

![The main window, mid-design: a lit virtual stage above the clip timeline, with the show toolbar on top and the clip library on the right.](docs/manual/images/main-window.png)

---

## What it does

**Audio and click.** One stereo output carries three buses — backing track, metronome
click and a spoken voice guide — each with its own volume and mute. The click can be
sent to one side and the backing to the other, so the musicians hear the click on
in-ears while the room hears only the music. Optional spoken count-in, and an optional
per-song *guide* track (a full-mix reference used while designing, never played live).

**MIDI-driven lighting.** Each song owns a MIDI file, edited in the app as *clips* on a
timeline, one lane per patched fixture. A clip's note is a **verb** (set a channel, load
a scene, run a chase, black out), its MIDI channel selects **which fixture** the verb
applies to, and its velocity is that verb's parameter. Move, resize, fade, snap,
multi-select, drag from the clip library.

**A virtual stage.** Every patched fixture is drawn in real time from the DMX actually
being sent — beams, haze, smoke, and on machines with a usable OpenGL driver an HDR
renderer with volumetric fog. Fixtures are described by
[Open Fixture Library](https://open-fixture-library.org/) definitions: all 17 OFL
categories and all 43 capability types resolve, so how a light is drawn comes from what
its channels declare, never from its name.

**A software DMX controller.** A pure-Python controller emulating a hardware lighting
desk is built in. With the factory settings it runs in-process and feeds the stage
directly, so **the app is fully usable with no hardware at all** — no interface, no MIDI
cable, no lights. When there *is* a rig, the same controller drives it over Art-Net or a
USB DMX interface.

**A tablet remote.** An optional web page on the local network shows the setlist, the
current section, a visual metronome, a scrubbable waveform and a grid of manual cues.
It is how the show is actually run from the stage.

**Shows are folders.** A show is `MyBand2026.show/` — a manifest, the MIDI files it
owns, any audio that came in with it, show-local fixture definitions and a `versions/`
history. Autosaved and snapshotted; exportable as a self-contained `.showpack` bundle.

---

## Install and run

Dependencies are managed with [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/lothwen/tupperDMX-simulator.git
cd tupperDMX-simulator

uv sync                            # install deps + create .venv
uv run python main.py              # run the app
uv run python main.py --headless   # no GUI (SBC/rack): the web remote is the interface
```

Prefer a binary? Grab one from the
[latest release](https://github.com/lothwen/tupperDMX-simulator/releases/latest) —
Linux, Windows and macOS builds are produced by CI on tagged releases.

**Requirements:** Python 3.13+ (only when running from source) and an audio output
device. Lighting hardware, MIDI hardware and a GPU are all optional.

---

## Three ways to run a show

| | Plays the audio | Makes the DMX | Operator touches |
|---|---|---|---|
| **Mode 1** | BBeat player | `tupperDMX-arduino` | the player — this app is the *design tool* |
| **Mode 2** | TupperDMX Simulator | TupperDMX Simulator | a laptop on stage |
| **Mode 3** | TupperDMX Simulator | TupperDMX Simulator | a tablet, over the web remote |

The internal software controller is a port of the Arduino firmware's logic, so a note
that produces a look here produces the same look there. See
[chapter 2](docs/manual/02-operating-modes.md) for the signal chains.

---

## Documentation

**[📕 User manual (PDF)](docs/Manual_TupperDMX.pdf)** — the whole thing in one file.
Or read it a chapter at a time:

| | |
|---|---|
| [1. Introduction](docs/manual/01-introduction.md) | what it is, and the five words you need |
| [2. Operating modes](docs/manual/02-operating-modes.md) | the three signal chains, and which one is yours |
| [3. The main interface](docs/manual/03-main-interface.md) | the window, panel by panel |
| [4. The stage view](docs/manual/04-stage-view.md) | placing, rigging and aiming the rig |
| [5. The Add fixture dialog](docs/manual/05-add-fixture-dialog.md) | the library, modes, channels and patching |
| [6. The timeline](docs/manual/06-timeline.md) | clips, tracks, scenes and blackouts |
| [7. The top toolbar](docs/manual/07-top-toolbar.md) | shows, setlist, song files, versions |
| [8. Clip library and DMX monitor](docs/manual/08-clip-library-and-dmx.md) | auditioning cues, watching the universe |
| [9. Settings](docs/manual/09-settings.md) | audio, I/O routing, stage, web remote, logging |
| [10. The web remote](docs/manual/10-web-remote.md) | running the night from a tablet |
| [11. A show from start to finish](docs/manual/11-example-workflow.md) | the worked example |
| [Appendix A. MIDI notes](docs/manual/12-appendix-midi-notes.md) | the note-map reference |

**For contributors:** [BUILD.md](BUILD.md) (packaging and CI) ·
[ROADMAP.md](ROADMAP.md) (what's next) ·
[CLAUDE.md](CLAUDE.md) (architecture, layering rules and the reasoning behind them) ·
[docs/manual/README.md](docs/manual/README.md) (rebuilding the manual and its screenshots)

```bash
uv run pytest        # headless test suite (offscreen Qt)
make build           # standalone executable in dist/
```

---

## Platform support

| Platform | Status |
|----------|--------|
| Linux    | Supported |
| Windows  | Supported |
| macOS    | Supported |

---

## License

[WTFPL](LICENSE.txt) — do what the fuck you want to.
