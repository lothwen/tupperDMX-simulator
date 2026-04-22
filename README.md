# TupperDMX Simulator

A PyQt6-based lighting control application that synchronizes MP3 audio playback with MIDI-triggered DMX lighting sequences. Visualizes DMX-controlled LED fixtures and blinders in real-time on a virtual stage.

## Features

### Audio & Playback
- MP3 audio playback synchronized to a MIDI timeline
- Drift-free metronome with configurable BPM and beat subdivision
- High/low pitch distinction for downbeats
- Audio offset configuration for fine-tuning sync
- Playlist management with previous/next/remove navigation

### MIDI Control
- Load and play MIDI files synchronized to audio
- Interactive MIDI event editing — drag, add, delete, and reposition events on the timeline
- Real-time MIDI output to hardware ports or the built-in software controller

### DMX Lighting Output
- **Art-Net (UDP)**: Send DMX data over the network to Art-Net-compatible fixtures
- **USB DMX**: Direct output via FTDI/serial USB DMX interfaces
- Real-time DMX channel monitor showing all 512 channels

### Software-Emulated Controller
No hardware? No problem. When Art-Net is pointed at localhost, a pure-Python DMX controller activates automatically. Select "Software emulated" as the MIDI output port and MIDI notes drive the lighting engine directly:
- 7-channel LED bar with scene banks (FILLS, ANIMATIONS, TUBES, STROBES, MULTICOLOR, and more)
- Two blinder fixtures with independent scene control
- 53 MIDI note mappings covering scenes, colors, song parts, and fixture control
- Smooth scene blending and color palette generation (analogous, complementary, triadic modes)
- Chase sequences and animated lighting effects
- Song part markers: INTRO, VERSE, CHORUS, BRIDGE, SOLO, OUTRO, and more

### Virtual Stage Visualization
- Real-time rendering of LED bar and blinder fixtures
- Colors and intensity update live from DMX data

### Timeline
- Waveform visualization with beat markers
- Zoomable and pannable content view
- Piano roll with interactive note visualization

---

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager (recommended)
- Audio output device
- Optional: MIDI interface, Art-Net device, or USB DMX interface

---

## Installation & Running

### With uv (Recommended)

```bash
# Clone the repository
git clone https://github.com/lothwen/tupperDMX-simulator.git
cd tupperDMX-simulator

# Install dependencies
uv sync

# Run the application
uv run python main.py
```

### With pip

```bash
pip install -r requirements.txt
python main.py
```

---

## Quick Start

1. **Load an audio file** via the toolbar (MP3 supported)
2. **Load a MIDI file** to associate lighting cues with the timeline
3. **Choose a DMX output**:
   - For hardware: set Art-Net IP/port or select your USB DMX device
   - For software simulation: set Art-Net IP to `127.0.0.1` and select "Software emulated" as the MIDI port
4. **Set BPM** to match your track
5. **Press Play** — audio, MIDI, metronome, and DMX all run in sync

---

## Building Executables

```bash
# Install build dependencies
uv sync --extra build

# Build for current platform
make build
```

Produces a standalone executable in `dist/`. Cross-platform builds (Windows, macOS, Linux) are automated via GitHub Actions on tagged releases.

---

## Platform Support

| Platform | Status |
|----------|--------|
| Linux    | Supported |
| Windows  | Supported |
| macOS    | Supported |

---

## License

See [LICENSE](LICENSE) for details.
