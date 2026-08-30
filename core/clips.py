# ABOUTME: Qt-free clip model — the descriptors the library panel renders as
# ABOUTME: cards, built data-driven from the patched fixtures' definitions.
"""Clip descriptors: what the fixture patch makes available to author with.

A *clip* is one authorable gesture — a channel-control verb, a colour macro, a
scene, a chase, a show-level verb — described as plain data:

    {"title", "type", "accent", "mime_type", "payload", "preview", "sentinel"}

Nothing is stored: ``build_groups`` regenerates every descriptor from
``config.fixtures`` plus the fixture library, so a repatch or a sidecar edit
shows up on the next rebuild.

Qt-free on purpose, and NOT because the domain layer wants it: three widget-free
consumers need the same descriptors — the library panel renders them as cards,
the timeline decodes their drop payloads, and the web remote resolves the show's
favourites through them. A second implementation of "what messages does this
scene fire" is a second answer waiting to disagree with the first.
"""

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from core import note_layout
from core.fixture_library import (GROUP_COLORS, FixtureDef, color_slots,
                                  resolve_fixture)
from core.note_layout import TAKE_MANUAL_VELOCITY
from software_controller.scene_loader import Scene, load_scenes

# Custom MIME type carries a JSON payload {"note": int, "velocity": int}.
# Plain-text fallback is also set so the payload is visible in tools that
# only inspect text/plain (handy for debugging the drop chain).
CLIP_MIME_TYPE = "application/x-tupperdmx-midi-clip"
DEFAULT_DROP_VELOCITY = 127

# MIME type for scene drops. The payload is a JSON object with the scene's
# name, per-channel value dict and owning fixture type; on drop the timeline
# expands it into one channel-control note_on per channel, sharing a group_id.
SCENE_MIME_TYPE = "application/x-tupperdmx-led-scene"

# Per-fixture group colours (the pill dot). Indexed by the fixture's position in
# the patch; every group belonging to a fixture — its channel page, each scene
# bank and its chases — shares that fixture's colour. The global Show group uses
# the neutral colour below.
# Shared with the timeline track headers so a fixture's swatch matches its
# library section (see core.fixture_library.GROUP_COLORS).
_GROUP_COLORS = GROUP_COLORS
SHOW_COLOR = '#888888'


# ---------------------------------------------------------------------------
# Clip references — the identity a favourite stores
# ---------------------------------------------------------------------------
# A ref names a clip SYMBOLICALLY, never as resolved MIDI. Storing the messages
# would freeze a scene's values at the moment it was starred and would re-address
# the wrong fixture after a repatch; a ref re-resolves through the same builder
# the library renders, so a favourite always fires what its card shows.
#
# The fixture is named by patch (type, mode) rather than by instance, matching
# the library itself: its groups are per type, and the MIDI channel is looked up
# from the first patched instance. `mode` keeps the PATCH's value — "" means the
# type's first declared mode, so renaming a mode never strands a favourite.
#
# A scene is keyed by (bank, NAME) with its index as a TIEBREAKER ONLY: the
# Scenes tab reorders scenes in the data, so an index-keyed favourite would
# silently start firing a different look, while a name-keyed one merely goes
# unresolved on rename. The index earns its place because bank names repeat in
# practice — the shipped ledbar pads FILLS2 with four identical "Fade In/Out
# slow" rows — and without it a star would light every one of them.

def note_ref(ftype: str, mode: str, note: int, velocity: int) -> Dict:
    """Ref for a per-fixture verb — channel control, colour macro, blackout,
    or a chase (note CHASE_LOAD, velocity = chase index)."""
    return {"kind": "note", "fixture": ftype, "mode": mode,
            "note": int(note), "velocity": int(velocity)}


def scene_ref(ftype: str, mode: str, bank: str, name: str,
              index: int) -> Dict:
    """Ref for one scene of a fixture's bank."""
    return {"kind": "scene", "fixture": ftype, "mode": mode,
            "bank": bank, "name": name, "index": int(index)}


def show_ref(note: int, velocity: int = DEFAULT_DROP_VELOCITY) -> Dict:
    """Ref for a global show verb (START, a song part, blackout-all), which
    belongs to no fixture."""
    return {"kind": "show", "note": int(note), "velocity": int(velocity)}


def parse_favorites(data: Any) -> List[Dict]:
    """Parse a show manifest's ``favorites`` list into normalized refs.

    Lenient like the rest of the loaders: an entry that isn't a well-formed ref
    of a known kind is dropped rather than failing the show, and every survivor
    is rebuilt through the constructors above so unmodelled keys don't ride
    along. Duplicates are collapsed — two identical refs would publish two
    identical buttons on the remote — keeping first-seen order, which IS the
    grid order."""
    out: List[Dict] = []
    seen = set()
    for entry in data if isinstance(data, list) else []:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        try:
            if kind == "note":
                ref = note_ref(str(entry["fixture"]), str(entry.get("mode", "")),
                               int(entry["note"]), int(entry["velocity"]))
            elif kind == "scene":
                ref = scene_ref(str(entry["fixture"]), str(entry.get("mode", "")),
                                str(entry["bank"]), str(entry["name"]),
                                int(entry.get("index", -1)))
            elif kind == "show":
                ref = show_ref(int(entry["note"]),
                               int(entry.get("velocity", DEFAULT_DROP_VELOCITY)))
            else:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        key = ref_key(ref)
        if key not in seen:
            seen.add(key)
            out.append(ref)
    return out


def ref_key(ref: Dict) -> Tuple:
    """Hashable identity of a ref — refs travel as JSON dicts, so equality
    needs a canonical tuple. An unknown kind keys on nothing and simply never
    matches a built clip."""
    kind = ref.get("kind")
    if kind == "note":
        return ("note", ref.get("fixture"), ref.get("mode", ""),
                int(ref.get("note", -1)), int(ref.get("velocity", -1)))
    if kind == "scene":
        return ("scene", ref.get("fixture"), ref.get("mode", ""),
                ref.get("bank"), ref.get("name"), int(ref.get("index", -1)))
    if kind == "show":
        return ("show", int(ref.get("note", -1)),
                int(ref.get("velocity", DEFAULT_DROP_VELOCITY)))
    return ("?",)


def describe_ref(ref: Dict) -> Tuple[str, str]:
    """Best-effort ``(title, reason)`` for a ref that did NOT resolve, so a
    dead favourite can still say what it was and why it is dead.

    The fixture DEFINITION is looked up even though the fixture is not patched
    — being absent from the patch is the usual reason a favourite goes dark,
    and the def is what knows the verb's real name."""
    kind = ref.get("kind")
    if kind == "show":
        info = note_layout.describe_note(int(ref.get("note", -1)))
        return (info.part if info else "Show verb", "verb no longer exists")

    ftype = str(ref.get("fixture", ""))
    fdef = resolve_fixture(ftype, str(ref.get("mode", "")))
    if kind == "scene":
        name = str(ref.get("name", "Scene"))
        if fdef is None:
            return (name, f"'{ftype}' is not in the fixture library")
        return (name, f"'{ref.get('bank')}' has no scene named '{name}'"
                if not any(sc.bank == ref.get("bank") and sc.name == name
                           for sc in load_scenes(ftype, str(ref.get("mode", ""))))
                else f"'{ftype}' is not patched in this show")
    if kind == "note":
        note = int(ref.get("note", -1))
        info = note_layout.describe_note(note, fdef,
                                         int(ref.get("velocity", 0))
                                         if note == note_layout.CHASE_LOAD else None)
        if fdef is None:
            return (f"Note {note}", f"'{ftype}' is not in the fixture library")
        return (info.part if info else f"Note {note}",
                f"'{ftype}' is not patched in this show")
    return ("Unknown", "unrecognised favourite")


def index_by_ref(groups: List[Dict]) -> Dict[Tuple, Dict]:
    """Every clip in ``groups``, keyed by its ref."""
    return {ref_key(clip["ref"]): clip
            for group in groups for clip in group["clips"]}


def _scene_name_key(ref: Dict) -> Tuple:
    return (ref.get("fixture"), ref.get("mode", ""), ref.get("bank"),
            ref.get("name"))


def resolve_in(groups: List[Dict], refs: List[Dict]) -> List[Optional[Dict]]:
    """Resolve ``refs`` against already-built ``groups``, in order, with one
    None per ref that no longer names a clip — the fixture was unpatched, the
    scene renamed, or the definition is gone. Never raises and never drops: an
    unresolvable favourite is a display state (dimmed, unfireable), because
    repatching the fixture has to bring it back.

    A scene whose exact key misses is retried on (fixture, mode, bank, name)
    alone, which is what carries a favourite across a reorder of its bank.

    Returns the descriptor OBJECTS out of ``groups``, so a caller can mark the
    starred cards by identity instead of re-deriving the match."""
    exact = index_by_ref(groups)
    by_name: Dict[Tuple, List[Dict]] = defaultdict(list)
    for group in groups:
        for clip in group["clips"]:
            if clip["ref"].get("kind") == "scene":
                by_name[_scene_name_key(clip["ref"])].append(clip)

    out: List[Optional[Dict]] = []
    for ref in refs:
        clip = exact.get(ref_key(ref))
        if clip is None and ref.get("kind") == "scene":
            candidates = by_name.get(_scene_name_key(ref))
            clip = candidates[0] if candidates else None
        out.append(clip)
    return out


def resolve_all(config, refs: List[Dict]) -> List[Optional[Dict]]:
    """``resolve_in`` against a freshly built set of groups."""
    return resolve_in(build_groups(config), refs)


def resolve(config, ref: Dict) -> Optional[Dict]:
    """Single-ref ``resolve_all`` — rebuilds the groups, so prefer
    ``resolve_all`` when resolving a whole favourites list."""
    return resolve_all(config, [ref])[0]

# ---------------------------------------------------------------------------
# Group model
# ---------------------------------------------------------------------------
def patched_types(config) -> List[Tuple[str, str]]:
    """Unique patched (fixture type, mode) pairs, deduped, ordered by DMX
    address (matching the DMX-sorted timeline).

    Mode is part of the identity: the same type in two modes exposes
    different channels, so each needs its own group of clips."""
    seen: List[Tuple[str, str]] = []
    for inst in sorted(config.fixtures, key=lambda f: f.dmx_start):
        t = getattr(inst, "type", None)
        if t and (t, getattr(inst, "mode", "")) not in seen:
            seen.append((t, getattr(inst, "mode", "")))
    return seen


def fixture_index_for_type(config, ftype: str, mode: str = "") -> int:
    """Stable MIDI channel of the first patched fixture of ``ftype`` in
    ``mode`` (by DMX address, matching the displayed group). The channel
    selects which fixture handle_note_on targets; defaults to 0 if the type
    isn't patched."""
    for inst in sorted(config.fixtures, key=lambda f: f.dmx_start):
        if (getattr(inst, "type", None) == ftype
                and getattr(inst, "mode", "") == mode):
            return getattr(inst, "channel", 0)
    return 0


def build_groups(config) -> List[Dict]:
    """Ordered group descriptors ``{label, color, clips}``: per patched
    fixture a channel-controls group, one group per scene bank and a chases
    group, all sharing the fixture's colour, plus a global Show group."""
    groups: List[Dict] = []
    for i, (ftype, mode) in enumerate(patched_types(config)):
        fdef = resolve_fixture(ftype, mode)
        if fdef is None:
            continue
        color = _GROUP_COLORS[i % len(_GROUP_COLORS)]
        channel = fixture_index_for_type(config, ftype, mode)

        clips = _fixture_clips(fdef, ftype, mode, channel)
        if clips:
            groups.append({"label": ftype.replace("_", " ").capitalize(),
                           "color": color, "clips": clips})
        for bank, bank_clips in _bank_groups(ftype, mode, fdef, channel):
            groups.append({"label": bank.replace("_", " ").capitalize(),
                           "color": color, "clips": bank_clips})
        chase_clips = _chase_clips(fdef, ftype, mode, channel)
        if chase_clips:
            groups.append({"label": "Chases", "color": color,
                           "clips": chase_clips})

    show_clips = _show_clips()
    if show_clips:
        groups.append({"label": "Show", "color": SHOW_COLOR,
                       "clips": show_clips})
    return groups


def _fixture_clips(fdef: FixtureDef, ftype: str, mode: str,
                   channel: int) -> List[Dict]:
    """A fixture's individual verbs: channel controls + per-fixture blackout
    + colour macros."""
    notes: List[int] = list(range(1, fdef.channel_count + 1))
    notes.append(note_layout.BLACKOUT_FIXTURE)
    notes += list(range(note_layout.COLOR_MACRO_BASE,
                        note_layout.COLOR_MACRO_BASE
                        + len(note_layout.COLOR_MACROS)))
    clips: List[Dict] = []
    for note in notes:
        info = note_layout.describe_note(note, fdef)
        if info is None:
            continue
        clips.append(_note_clip(info, note, DEFAULT_DROP_VELOCITY,
                                channel, clip_type(note),
                                note_ref(ftype, mode, note,
                                         DEFAULT_DROP_VELOCITY)))
    return clips


def _bank_groups(ftype: str, mode: str, fdef: FixtureDef, channel: int
                 ) -> List[Tuple[str, List[Dict]]]:
    """``(bank_name, [clip, ...])`` per scene bank of the fixture."""
    by_bank: Dict[str, List[Scene]] = defaultdict(list)
    for scene in load_scenes(ftype, mode):
        by_bank[scene.bank].append(scene)
    out: List[Tuple[str, List[Dict]]] = []
    for bank in sorted(by_bank.keys()):
        clips = [_scene_clip(fdef, ftype, mode, scene, channel)
                 for scene in sorted(by_bank[bank], key=lambda s: s.index)]
        out.append((bank, clips))
    return out


def _chase_clips(fdef: FixtureDef, ftype: str, mode: str,
                 channel: int) -> List[Dict]:
    """The fixture's chases (drop as note CHASE_LOAD, velocity = index)."""
    clips: List[Dict] = []
    for idx in range(len(fdef.chases)):
        info = note_layout.describe_note(note_layout.CHASE_LOAD, fdef, idx)
        clips.append(_note_clip(info, note_layout.CHASE_LOAD, idx,
                                channel, "chase",
                                note_ref(ftype, mode,
                                         note_layout.CHASE_LOAD, idx)))
    return clips


def _show_clips() -> List[Dict]:
    """Show-control verbs that route globally (no fixture): START, the song
    parts and blackout-all."""
    notes = ([note_layout.START]
             + list(range(note_layout.SONG_PART_BASE,
                          note_layout.SONG_PART_BASE
                          + len(note_layout.SONG_PARTS)))
             + [note_layout.BLACKOUT_ALL])
    clips: List[Dict] = []
    for note in notes:
        info = note_layout.describe_note(note)
        if info is None:
            continue
        clips.append(_note_clip(info, note, DEFAULT_DROP_VELOCITY, 0,
                                clip_type(note), show_ref(note)))
    return clips


# ---------------------------------------------------------------------------
# Clip descriptors
# ---------------------------------------------------------------------------
def clip_type(note: int) -> str:
    """Short clip-type subtitle for a single-note clip."""
    n = int(note)
    if n in (note_layout.BLACKOUT_FIXTURE, note_layout.BLACKOUT_ALL):
        return "blackout"
    last_macro = (note_layout.COLOR_MACRO_BASE
                  + len(note_layout.COLOR_MACROS) - 1)
    if note_layout.COLOR_MACRO_BASE <= n <= last_macro:
        return "color"
    if n == note_layout.START or note_layout.is_song_part(n):
        return "cue"
    if n == note_layout.CHASE_LOAD:
        return "chase"
    return "channel"


def _note_clip(info, note: int, velocity: int, channel: int,
               ctype: str, ref: Dict) -> Dict:
    payload = {"note": int(note), "velocity": int(velocity),
               "channel": int(channel)}
    return {"title": info.part, "type": ctype, "accent": info.color,
            "mime_type": CLIP_MIME_TYPE, "payload": payload,
            "preview": [(int(channel), int(note), int(velocity))],
            "sentinel": False, "ref": ref}


def _scene_clip(fdef: FixtureDef, ftype: str, mode: str, scene: Scene,
                channel: int) -> Dict:
    r, g, b = note_layout.scene_rgb(fdef, scene.values)
    # Lead with take-manual, exactly as add_scene folds it into a dropped
    # scene: without it the auto/song-part program overwrites the channel
    # values on its next frame and the audition shows nothing.
    preview: List[Tuple[int, int, int]] = [
        (int(channel), note_layout.TAKE_MANUAL, TAKE_MANUAL_VELOCITY)]
    for ch_name, note in note_layout.field_note_map(fdef):
        if ch_name not in scene.values:
            continue
        # Same lossy 0..255 -> 0..127 mapping the controller doubles back.
        velocity = max(0, min(127, int(scene.values[ch_name]) // 2))
        preview.append((channel, note, velocity))
    return {"title": scene.name, "type": "scene",
            "accent": f"#{r:02x}{g:02x}{b:02x}",
            "mime_type": SCENE_MIME_TYPE,
            "payload": _scene_to_payload(scene),
            "preview": preview,
            "sentinel": _is_palette_sentinel(fdef, scene.values),
            "ref": scene_ref(ftype, mode, scene.bank, scene.name,
                             scene.index)}


def _scene_to_payload(scene: Scene) -> Dict:
    return {
        "name": scene.name,
        "channels": dict(scene.values),
        "fixture_type": scene.fixture_type,
    }


def _is_palette_sentinel(fdef: FixtureDef, values: Dict[str, int]) -> bool:
    """Whether a scene hands its colour to the live palette — the same
    R/G/B-at-full rule `DMXController.load_scene` applies."""
    slots = color_slots(fdef)
    fields = [fdef.channels[slots[colour]].field
              for colour in ("Red", "Green", "Blue") if colour in slots]
    return len(fields) == 3 and all(int(values.get(f, 0)) >= 254
                                    for f in fields)
