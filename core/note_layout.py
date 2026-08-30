# ABOUTME: Reverse of handle_note_on — given a MIDI note (+ optional fixture def
# ABOUTME: and velocity) return a UI label/color. Single source of the note layout.
"""Note-layout lookup shared by the authoring UI and the runtime controller.

``software_controller/main.py``'s ``handle_note_on`` is the authority on what
each MIDI note *does* (the pitch is a universal verb, the channel selects the
fixture). This module is the *reverse*: it turns a note (resolved against a
fixture's own def when the verb is per-fixture) into the label/colour the clip
library, timeline and edit dialog show. The range constants below MIRROR the
match arms in ``handle_note_on`` — keep the two in sync.

Channel-control verbs (notes 1-23) name and colour themselves from the
fixture's own OFL capabilities via ``channel_families``, and a scene's swatch
comes from ``resolve_state`` — the same two sources the stage renders from, so
a clip can never claim a colour the fixture won't emit.

Qt-free on purpose: imported by both the controller and the UI without a cycle.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from core.channel_families import describe_channel
from core.fixture_library import FixtureDef
from core.fixture_state import resolve_state

# ---------------------------------------------------------------------------
# Note layout — MIRRORS software_controller/main.py:handle_note_on.
# ---------------------------------------------------------------------------
CHANNEL_OFFSET_RANGE = (1, 23)   # set the n-th DMX channel = velocity*2
SCENE_LOAD_RANGE = (24, 35)      # load scene (n-24) in the current bank
BANK_SELECT_RANGE = (36, 47)     # select bank (n-36)
TAKE_MANUAL = 48                 # take manual control of the fixture
# Velocity for the take-manual verb folded into each scene group. The runtime
# ignores it (take_manual_control reads no parameter), but it must be non-zero
# so the codec doesn't treat the note_on as a note_off. Matches the value the
# v2 MIDI migration injects.
TAKE_MANUAL_VELOCITY = 64
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
FIXTURE_SETUP = 71               # per-track setup flags packed in velocity
                                 # (auto on/off now; mute/solo reserved). Lives
                                 # in the .mid for persistence but describe_note
                                 # returns None so it never renders as a clip.
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

# Floor on a scene swatch's brightness, so a near-black scene still reads
# against the dark track background instead of vanishing into it.
_MIN_SWATCH_INTENSITY = 0.18


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


def is_fixture_setup(note: int) -> bool:
    """True for the per-track setup note (note 71)."""
    return int(note) == FIXTURE_SETUP


# Setup-note velocity bitfield. Each fixture's persisted track setup is packed
# into one note-71 velocity (mirrors the START note packing palette into
# velocity): `auto` (follows the song-part program), `mute` (its MIDI output is
# suppressed), and `lock` (its clips are read-only in the timeline). Solo stays
# reserved. handle_note_on treats note 71 as a NOP, so these bits are pure
# persistence and never affect the live output when the note replays.
SETUP_AUTO = 0x01
SETUP_MUTE = 0x02
SETUP_LOCK = 0x04


def pack_setup(*, auto: bool = False, mute: bool = False,
               lock: bool = False) -> int:
    """Pack the per-track setup flags into a MIDI velocity (0-127)."""
    v = 0
    if auto:
        v |= SETUP_AUTO
    if mute:
        v |= SETUP_MUTE
    if lock:
        v |= SETUP_LOCK
    return v


def unpack_setup(velocity: int) -> Dict[str, bool]:
    """Unpack a setup-note velocity into ``{"auto", "mute", "lock"}`` bools."""
    v = int(velocity)
    return {
        "auto": bool(v & SETUP_AUTO),
        "mute": bool(v & SETUP_MUTE),
        "lock": bool(v & SETUP_LOCK),
    }


def is_global_verb(note: int) -> bool:
    """True for notes ``handle_note_on`` dispatches *before* the channel check —
    i.e. channel-agnostic verbs (blackout-all and the song-part markers). START
    and the colour macros are per-fixture (they run after the channel selects
    the fixture), so they are NOT global."""
    n = int(note)
    return n == BLACKOUT_ALL or is_song_part(n)


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
            return NoteInfo(*describe_channel(fdef.channels[n - 1]))
        return NoteInfo(f'CH {n}', _NEUTRAL)
    if _in(SCENE_LOAD_RANGE, n):
        idx = n - SCENE_LOAD_RANGE[0]
        return NoteInfo(_scene_name(fdef, idx) or f'Scene {idx}', _NEUTRAL)
    if _in(BANK_SELECT_RANGE, n):
        idx = n - BANK_SELECT_RANGE[0]
        return NoteInfo(_bank_name(fdef, idx) or f'Bank {idx}', _NEUTRAL)

    # 0, 76-127: reserved / NOP. Note 71 (FIXTURE_SETUP) is intentionally None
    # here: it persists per-track setup flags in the .mid but must never render
    # as a timeline clip (the codec keeps it in midi_notes, drops it from
    # midi_events).
    return None


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


# Which group of the authoring picker a verb belongs to, in the order they are
# offered. Mirrors the match arms of handle_note_on, read as a menu.
_VERB_GROUPS: Tuple[Tuple[str, Tuple[int, ...]], ...] = (
    ("Channels", tuple(range(CHANNEL_OFFSET_RANGE[0], CHANNEL_OFFSET_RANGE[1] + 1))),
    ("Scenes", tuple(range(SCENE_LOAD_RANGE[0], SCENE_LOAD_RANGE[1] + 1))
     + (SCENE_PREV, SCENE_NEXT, SCENE_PACKED)),
    ("Banks", tuple(range(BANK_SELECT_RANGE[0], BANK_SELECT_RANGE[1] + 1))),
    ("Chases", (CHASE_LOAD,)),
    ("Control", (TAKE_MANUAL, MANUAL, BLACKOUT_FIXTURE, BLEND_TIME,
                 COLOR_CHANGE_TIME, CHASE_CHANGE_TIME,
                 PALETTE_RANDOM_ON, PALETTE_RANDOM_OFF)),
    ("Colour", tuple(range(COLOR_MACRO_BASE,
                           COLOR_MACRO_BASE + len(COLOR_MACROS)))),
    ("Show", (START, BLACKOUT_ALL)
     + tuple(range(SONG_PART_BASE, SONG_PART_BASE + len(SONG_PARTS)))),
)


def verb_choices(fdef: Optional[FixtureDef], *,
                 include_song_parts: bool = False,
                 include_channels: bool = True
                 ) -> List[Tuple[str, List[Tuple[int, str, str]]]]:
    """Grouped ``(group label, [(note, label, colour), ...])`` for every verb
    worth authoring on `fdef` — the authoring counterpart of the runtime
    dispatch, and what the clip editor's type picker is built from.

    Filtered by ``note_applies_to``, so a fixture with no banks is never offered
    a scene verb and a 6-channel par is never offered channel 7. Labelled by
    ``describe_note``, so the picker names the fixture's own channels, banks and
    chases rather than "CH 3".

    Song parts are excluded by default: they are global verbs that route with no
    channel, and ``edit_event`` never clears one, so offering them on a fixture
    lane would let a clip become a channel-scoped song part — a shape nothing
    else in the app produces. The waveform band, where they DO belong, asks for
    them explicitly.
    """
    out: List[Tuple[str, List[Tuple[int, str, str]]]] = []
    for label, notes in _VERB_GROUPS:
        entries: List[Tuple[int, str, str]] = []
        for note in notes:
            if not include_channels and _in(CHANNEL_OFFSET_RANGE, note):
                continue
            if is_song_part(note) and not include_song_parts:
                continue
            if not note_applies_to(note, fdef) or not _verb_resolves(note, fdef):
                continue
            info = describe_note(note, fdef)
            if info is None:
                continue
            entries.append((note, info.part, info.color))
        if entries:
            out.append((label, entries))
    return out


def _verb_resolves(note: int, fdef: Optional[FixtureDef]) -> bool:
    """Whether an INDEXED verb points at something the fixture actually has.

    ``note_applies_to`` answers the coarser question the drop validator needs
    ("is a scene verb meaningful on this fixture at all"), so it says yes to all
    twelve scene slots the moment a fixture has one bank. A picker has to be
    exact, or it offers "Scene 9" on a fixture with three.
    """
    n = int(note)
    if fdef is None:
        return True
    if _in(SCENE_LOAD_RANGE, n):
        first = next(iter(fdef.banks.values()), [])
        return n - SCENE_LOAD_RANGE[0] < len(first)
    if _in(BANK_SELECT_RANGE, n):
        return n - BANK_SELECT_RANGE[0] < len(fdef.banks)
    return True


# ---------------------------------------------------------------------------
# Scene-expansion helpers (per-fixture channel <-> note mapping).
# ---------------------------------------------------------------------------
def field_note_map(fdef: FixtureDef) -> Tuple[Tuple[str, int], ...]:
    """``((channel_field, midi_note), ...)`` for a fixture: note *i+1* drives the
    *i*-th channel, mirroring ``set_channel_offset`` (note n -> channel n).

    Capped at ``CHANNEL_OFFSET_RANGE``'s top: note 24 up is the scene-load verb,
    so emitting one for a 30-channel fixture's 24th slot would make
    ``handle_note_on`` LOAD A SCENE instead of setting a channel. A fixture
    wider than 23 slots simply has its tail unreachable from the timeline —
    which is what the editor greys out — rather than silently misfiring.
    """
    limit = CHANNEL_OFFSET_RANGE[1]
    return tuple((ch.field, i + 1)
                 for i, ch in enumerate(fdef.channels) if i + 1 <= limit)


def _swatch(fdef: FixtureDef, dmx: List[int]) -> Tuple[int, int, int]:
    """A scene's swatch: the colour the stage would render it, floored.

    Goes through the same `resolve_state` the stage does, so a scene's chip in
    the clip library and its clip on the timeline show the colour the fixture
    will actually emit — including from colours no swatch table would know about
    (amber, UV, a colour preset, a colour temperature). Multi-cell fixtures
    collapse to the brightest of their cells, since one chip stands for the
    whole fixture.
    """
    state = resolve_state(fdef, dmx)
    rgb = [0, 0, 0]
    for emitter in state.emitters:
        rgb = [max(channel, lit) for channel, lit in zip(rgb, emitter.rgb)]
    scale = max(state.intensity, _MIN_SWATCH_INTENSITY)
    return tuple(int(channel * scale) for channel in rgb)


def scene_rgb(fdef: FixtureDef, values_by_field: Dict[str, int]
              ) -> Tuple[int, int, int]:
    """Swatch RGB for a scene given its ``{channel_field: 0..255}`` values."""
    return _swatch(fdef, [max(0, min(255, int(values_by_field.get(ch.field, 0))))
                          for ch in fdef.channels])


def scene_rgb_from_notes(fdef: FixtureDef, velocity_by_note: Dict[int, int]
                         ) -> Tuple[int, int, int]:
    """Swatch RGB for an in-timeline scene group: ``{note: velocity}`` where
    channel value == velocity*2 (the controller's convention)."""
    return _swatch(fdef, [min(255, velocity_by_note.get(note, 0) * 2)
                          for _field, note in field_note_map(fdef)])


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
