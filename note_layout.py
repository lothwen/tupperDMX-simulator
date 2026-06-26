# ABOUTME: Reverse of handle_note_on — given a MIDI note (+ optional fixture def
# ABOUTME: and velocity) return a UI label/color. Single source of the note layout.
"""Note-layout lookup shared by the authoring UI and the runtime controller.

``software_controller/main.py``'s ``handle_note_on`` is the authority on what
each MIDI note *does* (the pitch is a universal verb, the channel selects the
fixture). This module is the *reverse*: it turns a note (resolved against a
fixture's own def when the verb is per-fixture) into the label/colour the clip
library, timeline and edit dialog show. The range constants below MIRROR the
match arms in ``handle_note_on`` — keep the two in sync (same convention as the
``ROLE_*`` constants shared with ``fixture_library``).

Qt-free on purpose: imported by both the controller and the UI without a cycle
(it depends only on ``fixture_library``).
"""

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from fixture_library import (
    FixtureDef,
    ROLE_GENERIC, ROLE_BRIGHTNESS, ROLE_RED, ROLE_GREEN, ROLE_BLUE,
    ROLE_WHITE_WARM, ROLE_WHITE_COOL, ROLE_ANIMATION, ROLE_SPEED,
    ROLE_DIRECTION, ROLE_STROBE, ROLE_MODE,
)

# ---------------------------------------------------------------------------
# Note layout — MIRRORS software_controller/main.py:handle_note_on.
# ---------------------------------------------------------------------------
CHANNEL_OFFSET_RANGE = (1, 23)   # set the n-th DMX channel = velocity*2
SCENE_LOAD_RANGE = (24, 35)      # load scene (n-24) in the current bank
BANK_SELECT_RANGE = (36, 47)     # select bank (n-36)
TAKE_MANUAL = 48                 # take manual control of the fixture
MANUAL = 49                      # drop to manual (hold current frame)
BLACKOUT_FIXTURE = 50            # blackout this fixture (per-fixture verb)
BLACKOUT_ALL = 51                # blackout everything (global verb)
BLEND_TIME = 52
COLOR_CHANGE_TIME = 53
CHASE_CHANGE_TIME = 54
PALETTE_RANDOM_ON = 55
PALETTE_RANDOM_OFF = 56
SCENE_PREV = 57
SCENE_NEXT = 58
SCENE_PACKED = 59
START = 60                       # palette init (global verb)
SONG_PART_BASE = 61              # 61-69 INTRO..END (global verbs)
CHASE_LOAD = 70                  # load chase (velocity = chase index) — NEW verb
COLOR_MACRO_BASE = 72            # 72-75 WHITE/ORANGE/CYAN/PINK

# Ordered names (match handle_note_on's SONG_PARTS / colour-macro cases).
SONG_PARTS = ['INTRO', 'VERSE', 'CHORUS', 'BREAK', 'BRIDGE',
              'INST', 'SOLO', 'OUTRO', 'END']
COLOR_MACROS = ['WHITE', 'ORANGE', 'CYAN', 'PINK']

# ---------------------------------------------------------------------------
# Colours / display names.
# ---------------------------------------------------------------------------
_NEUTRAL = '#555555'
_BLACKOUT_COLOR = '#363636'
_START_COLOR = '#c73737'

# Curated per-song-part marker colours (carried over from the old config.json
# midi_note_map so existing timelines keep their familiar palette).
_SONG_PART_COLORS = {
    'INTRO': '#c77f37', 'VERSE': '#c6c737', 'CHORUS': '#37c737',
    'BREAK': '#3737c7', 'BRIDGE': '#461c64', 'INST': '#802da4',
    'SOLO': '#ffffff', 'OUTRO': '#c79337', 'END': '#808080',
}

# Channel-control display name + swatch colour, by semantic role.
_ROLE_NAMES = {
    ROLE_GENERIC: 'Channel', ROLE_BRIGHTNESS: 'Brightness',
    ROLE_RED: 'Red', ROLE_GREEN: 'Green', ROLE_BLUE: 'Blue',
    ROLE_WHITE_WARM: 'Warm White', ROLE_WHITE_COOL: 'Cool White',
    ROLE_ANIMATION: 'Animation', ROLE_SPEED: 'Speed',
    ROLE_DIRECTION: 'Direction', ROLE_STROBE: 'Strobe', ROLE_MODE: 'Mode',
}
_ROLE_COLORS = {
    ROLE_RED: '#aa3333', ROLE_GREEN: '#33aa33', ROLE_BLUE: '#3333aa',
    ROLE_WHITE_WARM: '#c7a037', ROLE_WHITE_COOL: '#9fb6c7',
}

# Approximate RGB a white channel contributes to a scene swatch (so blinder
# scenes, which have no RGB channels, don't render as black).
_WHITE_WARM_RGB = (255, 170, 70)
_WHITE_COOL_RGB = (205, 220, 255)


@dataclass(frozen=True)
class NoteInfo:
    """UI metadata for one note. ``part`` keeps its legacy name because the
    timeline render layer reads ``info.part`` / ``info.color``."""
    part: str
    color: str


def _in(rng: Tuple[int, int], n: int) -> bool:
    return rng[0] <= n <= rng[1]


def is_song_part(note: int) -> bool:
    """True for the global song-part markers (notes 61-69)."""
    return SONG_PART_BASE <= int(note) <= SONG_PART_BASE + len(SONG_PARTS) - 1


def is_global_verb(note: int) -> bool:
    """True for notes ``handle_note_on`` dispatches *before* the channel check —
    i.e. channel-agnostic verbs (blackout-all and the song-part markers). START
    and the colour macros are per-fixture (they run after the channel selects
    the fixture), so they are NOT global."""
    n = int(note)
    return n == BLACKOUT_ALL or is_song_part(n)


def _role_name(ch) -> str:
    return _ROLE_NAMES.get(ch.role) or ch.field.replace('_', ' ').title()


def _role_color(ch) -> str:
    return _ROLE_COLORS.get(ch.role, _NEUTRAL)


def _bank_name(fdef: Optional[FixtureDef], idx: int) -> Optional[str]:
    if fdef and 0 <= idx < len(fdef.banks):
        return list(fdef.banks.keys())[idx]
    return None


def _scene_name(fdef: Optional[FixtureDef], scene_idx: int,
                bank: Optional[str] = None) -> Optional[str]:
    """Name of scene `scene_idx` in `bank` (or the first bank) of `fdef`."""
    if not fdef or not fdef.banks:
        return None
    bank = bank or next(iter(fdef.banks))
    scenes = fdef.banks.get(bank) or []
    if 0 <= scene_idx < len(scenes):
        s = scenes[scene_idx]
        return str(s.get('name') or f'{bank} #{scene_idx + 1}')
    return None


def _chase_name(fdef: Optional[FixtureDef], idx: int) -> Optional[str]:
    if fdef and 0 <= idx < len(fdef.chases):
        c = fdef.chases[idx]
        return str(c.get('name') or f'Chase {idx + 1}')
    return None


# Fixed labels for the per-fixture utility verbs (48-59).
_UTILITY_LABELS = {
    TAKE_MANUAL: 'TAKE MANUAL', MANUAL: 'MANUAL',
    BLEND_TIME: 'BLEND TIME', COLOR_CHANGE_TIME: 'COLOR TIME',
    CHASE_CHANGE_TIME: 'CHASE TIME', PALETTE_RANDOM_ON: 'PALETTE RANDOM',
    PALETTE_RANDOM_OFF: 'PALETTE FIXED', SCENE_PREV: 'SCENE PREV',
    SCENE_NEXT: 'SCENE NEXT', SCENE_PACKED: 'SCENE (packed)',
}


def describe_note(note: int, fdef: Optional[FixtureDef] = None,
                  velocity: Optional[int] = None) -> Optional[NoteInfo]:
    """Return label/colour for `note`, resolving per-fixture verbs against
    `fdef` (and `velocity` for scene/chase indices) when available.

    Returns None for notes with no defined meaning (reserved / NOP).
    """
    n = int(note)

    # ---- Global verbs (channel-agnostic) ----
    if n == BLACKOUT_ALL:
        return NoteInfo('BLACKOUT ALL', _BLACKOUT_COLOR)
    if n == START:
        return NoteInfo('START', _START_COLOR)
    if is_song_part(n):
        name = SONG_PARTS[n - SONG_PART_BASE]
        return NoteInfo(name, _SONG_PART_COLORS.get(name, _NEUTRAL))
    if _in((COLOR_MACRO_BASE, COLOR_MACRO_BASE + len(COLOR_MACROS) - 1), n):
        return NoteInfo(f'COLOR: {COLOR_MACROS[n - COLOR_MACRO_BASE]}', _NEUTRAL)

    # ---- Per-fixture verbs ----
    if n == BLACKOUT_FIXTURE:
        return NoteInfo('BLACKOUT', _BLACKOUT_COLOR)
    if n in _UTILITY_LABELS:
        return NoteInfo(_UTILITY_LABELS[n], _NEUTRAL)
    if n == CHASE_LOAD:
        idx = velocity if velocity is not None else None
        label = (_chase_name(fdef, idx) if idx is not None else None) \
            or (f'Chase {idx}' if idx is not None else 'CHASE')
        return NoteInfo(label, _NEUTRAL)
    if _in(CHANNEL_OFFSET_RANGE, n):
        if fdef and n <= fdef.channel_count:
            ch = fdef.channels[n - 1]
            return NoteInfo(_role_name(ch), _role_color(ch))
        return NoteInfo(f'CH {n}', _NEUTRAL)
    if _in(SCENE_LOAD_RANGE, n):
        idx = n - SCENE_LOAD_RANGE[0]
        return NoteInfo(_scene_name(fdef, idx) or f'Scene {idx}', _NEUTRAL)
    if _in(BANK_SELECT_RANGE, n):
        idx = n - BANK_SELECT_RANGE[0]
        return NoteInfo(_bank_name(fdef, idx) or f'Bank {idx}', _NEUTRAL)

    return None  # 0, 71, 76-127: reserved / NOP


def note_applies_to(note: int, fdef: Optional[FixtureDef]) -> bool:
    """True if `note` is a meaningful verb to author on a fixture of `fdef`.

    Global verbs apply anywhere. Channel offsets require the channel to exist;
    scene/bank/chase verbs require the fixture to define banks/chases.
    """
    n = int(note)
    if is_global_verb(n):
        return True  # blackout-all / song parts: any lane
    if _in(CHANNEL_OFFSET_RANGE, n):
        return bool(fdef and n <= fdef.channel_count)
    if _in(SCENE_LOAD_RANGE, n) or _in(BANK_SELECT_RANGE, n):
        return bool(fdef and fdef.banks)
    if n == CHASE_LOAD:
        return bool(fdef and fdef.chases)
    # START, colour macros, blackout-fixture, manual + timing verbs all run
    # against whatever fixture the channel selects, so they apply to any one.
    if n == START or n == BLACKOUT_FIXTURE or n in _UTILITY_LABELS:
        return True
    return _in((COLOR_MACRO_BASE, COLOR_MACRO_BASE + len(COLOR_MACROS) - 1), n)


# ---------------------------------------------------------------------------
# Scene-expansion helpers (per-fixture channel <-> note mapping).
# ---------------------------------------------------------------------------
def field_note_map(fdef: FixtureDef) -> Tuple[Tuple[str, int], ...]:
    """``((channel_field, midi_note), ...)`` for a fixture: note *i+1* drives the
    *i*-th channel, mirroring ``set_channel_offset`` (note n -> channel n)."""
    return tuple((ch.field, i + 1) for i, ch in enumerate(fdef.channels))


def channel_control_notes(fdef: FixtureDef) -> frozenset:
    """The channel-control note numbers (1..channel_count) of a fixture."""
    return frozenset(range(1, fdef.channel_count + 1))


def _rgb_from_values(fdef: FixtureDef, value_of: Callable[[str], int]
                     ) -> Tuple[int, int, int]:
    """Approximate a scene's RGB swatch from its per-channel values, using each
    channel's semantic role. ``value_of(field)`` returns the 0..255 value."""
    r = g = b = 0
    brightness = 255
    have_brightness = False
    for ch in fdef.channels:
        v = max(0, min(255, int(value_of(ch.field))))
        if ch.role == ROLE_RED:
            r = max(r, v)
        elif ch.role == ROLE_GREEN:
            g = max(g, v)
        elif ch.role == ROLE_BLUE:
            b = max(b, v)
        elif ch.role == ROLE_BRIGHTNESS:
            brightness = v
            have_brightness = True
        elif ch.role in (ROLE_WHITE_WARM, ROLE_WHITE_COOL):
            tint = _WHITE_WARM_RGB if ch.role == ROLE_WHITE_WARM else _WHITE_COOL_RGB
            r = max(r, tint[0] * v // 255)
            g = max(g, tint[1] * v // 255)
            b = max(b, tint[2] * v // 255)
    # Brightness scales the colour so dim scenes read dim; floor so near-black
    # scenes stay faintly visible against the dark track background.
    scale = max(brightness / 255.0, 0.18) if have_brightness else 1.0
    return int(r * scale), int(g * scale), int(b * scale)


def scene_rgb(fdef: FixtureDef, values_by_field: Dict[str, int]
              ) -> Tuple[int, int, int]:
    """Swatch RGB for a scene given its ``{channel_field: 0..255}`` values."""
    return _rgb_from_values(fdef, lambda f: values_by_field.get(f, 0))


def scene_rgb_from_notes(fdef: FixtureDef, velocity_by_note: Dict[int, int]
                         ) -> Tuple[int, int, int]:
    """Swatch RGB for an in-timeline scene group: ``{note: velocity}`` where
    channel value == velocity*2 (the controller's convention)."""
    field_to_note = {f: nt for f, nt in field_note_map(fdef)}

    def value_of(field: str) -> int:
        return min(255, velocity_by_note.get(field_to_note.get(field, -1), 0) * 2)

    return _rgb_from_values(fdef, value_of)


# ---------------------------------------------------------------------------
# Song-part program (the auto-mode arrangement, data-driven from "song_parts").
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SongPartRef:
    """One scene or chase a fixture runs for a song part, indexed from the
    ``"song_parts"`` tags on its def. For chases ``bank`` is "" and ``index`` is
    the chase index; for scenes ``bank``/``index`` locate the scene in a bank.
    Single source for both the runtime program builder
    (software_controller._build_song_part_program) and the timeline auto-mode
    visualization (MidiEventsTrack)."""
    is_chase: bool
    bank: str
    index: int


def song_part_refs(fdef: FixtureDef) -> Dict[str, List[SongPartRef]]:
    """``{part-name: [SongPartRef, ...]}`` indexed from the ``"song_parts"`` tags
    on `fdef`'s scenes and chases — i.e. what auto mode may run per song part."""
    program: Dict[str, List[SongPartRef]] = {}
    for bank_name, scenes in fdef.banks.items():
        for idx, scene in enumerate(scenes):
            for part in scene.get("song_parts", []):
                program.setdefault(part, []).append(SongPartRef(False, bank_name, idx))
    for idx, chase in enumerate(fdef.chases):
        for part in chase.get("song_parts", []):
            program.setdefault(part, []).append(SongPartRef(True, "", idx))
    return program


def song_part_clip_info(fdef: Optional[FixtureDef], part: str) -> Optional[NoteInfo]:
    """Label/colour for the auto-mode visualization clip of `part` on `fdef`.

    None when the fixture has nothing tagged for `part` (the lane shows no clip
    there). Otherwise the name of the FIRST tagged scene/chase (with a ``" +N"``
    suffix when several are tagged, since the runtime pick is random), coloured
    by the song part's own palette colour — the same per-part colour the
    waveform arrangement overlay uses, so every CHORUS/VERSE/etc. reads alike."""
    if fdef is None:
        return None
    refs = song_part_refs(fdef).get(part) or []
    if not refs:
        return None
    ref = refs[0]
    name = (_chase_name(fdef, ref.index) if ref.is_chase
            else _scene_name(fdef, ref.index, ref.bank)) or part
    if len(refs) > 1:
        name = f'{name} +{len(refs) - 1}'
    return NoteInfo(name, _SONG_PART_COLORS.get(part, _NEUTRAL))


# ---------------------------------------------------------------------------
# Fixture-agnostic note map (for the renderer / codec / edit dialog).
# ---------------------------------------------------------------------------
def build_note_map() -> Dict[str, NoteInfo]:
    """Materialise the notes that appear as *standalone* clips into a
    ``{note_str: NoteInfo}`` dict (the renderer / codec look notes up by number).

    Covers the fixture-agnostic verbs plus generic channel-control labels.
    Per-fixture scene/chase labels are resolved on demand via describe_note in
    the clip library, where the fixture def is known. BLACKOUT (50) is inserted
    before BLACKOUT ALL (51) so the document's prefix search picks the
    per-fixture marker first.
    """
    notes: List[int] = (
        list(range(CHANNEL_OFFSET_RANGE[0], 8))            # channel controls 1-7
        + [BLACKOUT_FIXTURE, BLACKOUT_ALL, START]          # blackout/start
        + list(range(SONG_PART_BASE, SONG_PART_BASE + len(SONG_PARTS)))
        + [CHASE_LOAD]
        + list(range(COLOR_MACRO_BASE, COLOR_MACRO_BASE + len(COLOR_MACROS)))
    )
    out: Dict[str, NoteInfo] = {}
    for n in notes:
        info = describe_note(n)
        if info is not None:
            out[str(n)] = info
    return out
