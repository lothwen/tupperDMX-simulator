# ABOUTME: A song's .mid played into the software controller on a simulated
# ABOUTME: clock, the way playback plays it, and each frame read back through
# ABOUTME: core.fixture_state: what every fixture emits — a light score's data.
"""The light a .mid makes, without playing it.

The song's .mid is played into the app's own software controller the way
playback plays it — its auto flags applied, muted lanes skipped, every note_on
handed to handle_note_on at its time — on a simulated clock, one frame every
1/20 s as the controller sends them. Chases, blends and palette cycling run as
they do live; the palette's random steps are seeded, so a song replays the
same every time. Each frame's DMX is read back through core.fixture_state, the
model the stage draws from, so this is what the stage would show, whoever
programmed the song.
"""
import logging
import random
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

import mido
import numpy as np

from core import fixture_library as fl
from core import note_layout
from core.fixture_state import resolve_state
from midi.manager import MidiManager
from software_controller.main import DMX_UPDATE_HZ, UNIVERSE_SIZE, DMXController
from tupperlogger import get_logger

FRAME = 1.0 / DMX_UPDATE_HZ


def replay(fixtures, midi_path: str, end: float) -> np.ndarray:
    """The universe every FRAME seconds from 0 to `end` (frames x 512), as
    the software controller puts it out while `midi_path` plays."""
    doc = MidiManager(note_layout.build_note_map(), fixtures=fixtures)
    doc.load_from_file(midi_path)
    # A clock of our own lets the song play faster than real time, and a
    # seeded generator makes the random palette steps the same every time.
    # In the app the controller has been running long before playback: its
    # start-up blend, which would wipe the song's first notes, is over — so
    # it starts a second early.
    clock = SimpleNamespace(now=-DMX_UPDATE_HZ * FRAME)
    controller = DMXController(fixtures, clock=lambda: clock.now, rng=random.Random(0))
    # The live controller logs every note it is handed; a replay hands it a
    # whole song in a moment, so this one keeps to warnings.
    controller.logger = get_logger("software_controller.replay")
    controller.logger.setLevel(logging.WARNING)
    controller.apply_auto(doc.auto_channels())
    muted = doc.mute_channels()
    messages, at = [], 0.0
    for msg in mido.MidiFile(midi_path):             # as midi/worker.py reads it
        at += msg.time
        if not msg.is_meta:
            messages.append((at, msg))

    for k in range(DMX_UPDATE_HZ, 0, -1):
        clock.now = -k * FRAME
        controller.tick_timers(clock.now)
        controller.tick_frame(FRAME * 1000)
    frames = np.zeros((int(np.ceil(end / FRAME)), UNIVERSE_SIZE), np.uint8)
    i = 0
    for k in range(len(frames)):
        clock.now = now = k * FRAME
        while i < len(messages) and messages[i][0] <= now:
            msg = messages[i][1]
            i += 1
            if msg.type == "note_on" and getattr(msg, "channel", None) not in muted:
                controller.handle_note_on(msg.channel, msg.note, msg.velocity)
        controller.tick_timers(now)
        frames[k] = np.clip(controller.tick_frame(FRAME * 1000), 0, 255)
    return frames


# ---------------------------------------------------------------------------
# Reading a frame back
# ---------------------------------------------------------------------------
class Strip:
    """One fixture's row: the patch entries on its DMX address, its def,
    and what it emits frame by frame."""

    def __init__(self, channels: List[int], fdef, start: int):
        self.channels, self.fdef, self.start = channels, fdef, start
        self.idle = resolve_state(fdef, [0] * fdef.channel_count).program.name
        self._seen: Dict[bytes, tuple] = {}

    @property
    def label(self) -> str:
        return f"ch {'+'.join(map(str, self.channels))}  {self.fdef.name}"

    def look(self, frame: np.ndarray) -> tuple:
        """``(emitter colours, strobing, animating, fog 0-1)`` for a frame."""
        dmx = bytes(frame[self.start:self.start + self.fdef.channel_count])
        if dmx not in self._seen:
            state = resolve_state(self.fdef, dmx)
            gel = state.color.filter_rgb
            colours = tuple(tuple(int(c * g / 255) for c, g in zip(e.scaled_rgb(), gel)) if gel
                            else e.scaled_rgb() for e in state.emitters) or (state.scaled_rgb(),)
            strobing = state.strobe.effect not in ("Open", "Closed") and state.intensity > 0
            animating = bool(state.program.name) and state.program.name != self.idle
            self._seen[dmx] = (colours, strobing, animating, state.atmosphere.output)
        return self._seen[dmx]


def strips_of(fixtures) -> List[Strip]:
    strips: Dict[tuple, Strip] = {}
    for inst in sorted(fixtures, key=lambda f: f.channel):
        fdef = fl.resolve_fixture(inst.type, inst.mode)
        if fdef is None:
            continue
        key = (inst.dmx_start, fdef.name, fdef.mode)
        if key in strips:
            strips[key].channels.append(inst.channel)
        else:
            strips[key] = Strip([inst.channel], fdef, inst.dmx_start - 1)
    return list(strips.values())


def loudness(rate: int, mono: np.ndarray, offset: float, columns: np.ndarray,
             px: float) -> np.ndarray:
    """The RMS of a track under each pixel column, 0-1."""
    out = np.zeros(len(columns))
    for k, t in enumerate(columns):
        a, b = int((t - offset) * rate), int((t - offset + 1 / px) * rate)
        if b > 0 and a < len(mono):
            chunk = mono[max(0, a):b]
            out[k] = float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) else 0.0
    return out / (np.percentile(out[out > 0], 98) if (out > 0).any() else 1.0)


@dataclass
class LightScore:
    """What a light score shows (score.py draws it): a strip per fixture with
    its frames, the song parts as (start, name), and the loudness under each
    pixel column — `px` columns a second, from `start` to `stop` seconds."""
    title: str
    strips: List[Strip]
    frames: np.ndarray
    parts: List[Tuple[float, str]]
    audio: Optional[np.ndarray]
    px: float
    start: float
    stop: float


def light_score(fixtures, title: str, midi_path: str, audio, px: float, start: float = 0.0,
                stop: Optional[float] = None) -> LightScore:
    """The light score of the .mid at `midi_path` on the patch `fixtures`, at
    `px` pixels a second; `audio` is the ``(rate, mono, offset)`` whose
    loudness is drawn below it, or None."""
    doc = MidiManager(note_layout.build_note_map(), fixtures=fixtures)
    doc.load_from_file(midi_path)
    parts = sorted((e["time"], note_layout.SONG_PARTS[e["note"] - note_layout.SONG_PART_BASE])
                   for e in doc.midi_events if note_layout.is_song_part(e["note"]))
    last_message = sum(msg.time for msg in mido.MidiFile(midi_path))
    end = max([last_message] + [at for at, _name in parts]) + 2.0
    stop = min(stop, end) if stop is not None else end
    frames = replay(fixtures, midi_path, stop)
    columns = start + np.arange(int((stop - start) * px)) / px
    level = loudness(*audio, columns, px) if audio is not None else None
    return LightScore(title, strips_of(fixtures), frames, parts, level, px, start, stop)
