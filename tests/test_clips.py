"""
Characterization test: the Qt-free clip model (core/clips.py).

Covers the group structure the library panel renders (per-fixture verbs, one
group per scene bank, chases, the global Show group), and the clip REFS a
favourite stores — uniqueness, round-tripping back to the same descriptor, the
scene fallback that survives a bank reorder, and the None every consumer must
treat as "dimmed, unfireable" rather than as a reason to drop the favourite.

Run with:
    uv run python tests/test_clips.py
"""

import os
import subprocess
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from core import clips, note_layout
from core.config import Config, FixtureInstance, shipped_fixtures


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _shipped_config() -> Config:
    config = Config()
    config.fixtures = shipped_fixtures()
    return config


def _all_clips(groups):
    return [clip for group in groups for clip in group["clips"]]


def test_qt_free():
    print("test_qt_free")
    # The library panel, the timeline's drop handler and the web remote all
    # resolve through this module; a Qt import here would put the whole widget
    # stack behind a headless web remote. Checked in a subprocess because the
    # shared test QApplication has already imported Qt into THIS one.
    probe = ("import sys; import core.clips; "
             "sys.exit(1 if 'PyQt6' in sys.modules else 0)")
    result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT,
                            capture_output=True, text=True)
    _assert(result.returncode == 0,
            f"importing core.clips pulls in no Qt ({result.stderr.strip()})")


def test_group_structure():
    print("test_group_structure")
    groups = clips.build_groups(_shipped_config())
    labels = [g["label"] for g in groups]

    _assert(labels[0] == "Ledbar" and labels[-1] == "Show",
            "the patch's first fixture leads and the global Show group closes")
    _assert("Animations" in labels and "Strobes" in labels,
            "each scene bank of a fixture becomes its own group")
    _assert(labels.count("Chases") == 2,
            "each patched type contributes its own Chases group")
    _assert(len({g["color"] for g in groups}) == 3,
            "one colour per patched fixture, plus the neutral Show colour")

    show = groups[-1]["clips"]
    _assert([c["title"] for c in show][:2] == ["INTRO", "VERSE"],
            "the Show group is the song parts, then blackout-all — START (60) "
            "is fixture state now, not a clip anyone can drag")
    _assert(not any(c["payload"]["note"] == note_layout.START for c in show),
            "no START clip is offered anywhere")
    _assert(all(c["ref"]["kind"] == "show" for c in show),
            "show verbs belong to no fixture")


def test_a_group_names_the_fixture_it_came_from():
    """A group's LABEL cannot answer "which fixture is this about?" — a bank
    group is named after the bank. The clip library has to know, to offer
    "Edit <fixture>…" on a card and to route its "+ New" tile."""
    print("test_a_group_names_the_fixture_it_came_from")
    config = _shipped_config()
    patched = {(f.type, getattr(f, "mode", "")) for f in config.fixtures}
    groups = clips.build_groups(config)

    for group in groups[:-1]:
        _assert((group.get("fixture"), group.get("mode", "")) in patched,
                f"'{group['label']}' names a (type, mode) that is patched")

    show = groups[-1]
    _assert(show["label"] == "Show", "the last group is the global one")
    _assert("fixture" not in show and "mode" not in show,
            "which names no fixture, so 'no single fixture' is answerable "
            "rather than inferred")


def test_clip_payloads():
    print("test_clip_payloads")
    groups = clips.build_groups(_shipped_config())
    by_type = {}
    for clip in _all_clips(groups):
        by_type.setdefault(clip["type"], clip)

    note = by_type["channel"]
    _assert(note["mime_type"] == clips.CLIP_MIME_TYPE
            and note["payload"]["velocity"] == clips.DEFAULT_DROP_VELOCITY,
            "a channel clip drags as a single note at the default velocity")

    scene = by_type["scene"]
    _assert(scene["mime_type"] == clips.SCENE_MIME_TYPE
            and scene["preview"][0][1] == 48,
            "a scene previews TAKE_MANUAL first, or the program overwrites it")
    _assert(len(scene["preview"]) > 1 and scene["payload"]["channels"],
            "a scene expands to its per-channel notes")

    chase = by_type["chase"]
    _assert(chase["payload"]["note"] == 70,
            "a chase drags as CHASE_LOAD with the index in the velocity")


def test_indexed_clips_name_their_fixture():
    """A channel clip's title is the SOURCE fixture's channel — "TILT" is this
    moving head's channel 1 and nothing at all on a par that merely also has a
    channel 1 — and a chase clip's velocity indexes that fixture's own chase
    list. Both carry the fixture they were built from, and the timeline refuses
    them on any other fixture's lane. The verbs addressed through the TARGET's
    patch (a colour macro, a blackout, a show verb) stay droppable anywhere they
    apply."""
    print("test_indexed_clips_name_their_fixture")
    lo, hi = note_layout.CHANNEL_OFFSET_RANGE
    scoped_clips, others = [], []
    for clip in _all_clips(clips.build_groups(_shipped_config())):
        if clip["mime_type"] != clips.CLIP_MIME_TYPE:
            continue  # a scene, which has always named its fixture
        ref = clip["ref"]
        scoped = ref.get("kind") == "note" and (
            lo <= ref["note"] <= hi or ref["note"] == note_layout.CHASE_LOAD)
        (scoped_clips if scoped else others).append(clip)

    _assert(scoped_clips and all(
        c["payload"].get("fixture_type") == c["ref"]["fixture"]
        for c in scoped_clips),
        f"all {len(scoped_clips)} channel and chase clips name their fixture")
    _assert(any(c["type"] == "chase" for c in scoped_clips),
            "chases are among them (the shipped patch has some)")
    _assert(not any("fixture_type" in c["payload"] for c in others),
            "blackout, colour macros and show verbs stay unscoped")


def test_refs_are_unique_and_round_trip():
    print("test_refs_are_unique_and_round_trip")
    config = _shipped_config()
    groups = clips.build_groups(config)
    every = _all_clips(groups)

    keys = {clips.ref_key(c["ref"]) for c in every}
    _assert(len(keys) == len(every),
            f"all {len(every)} clips have distinct refs (a star lights one card)")

    resolved = clips.resolve_in(groups, [c["ref"] for c in every])
    _assert(all(r is c for r, c in zip(resolved, every)),
            "every ref resolves back to its own descriptor, by identity")


def test_ref_keeps_the_patch_mode():
    print("test_ref_keeps_the_patch_mode")
    config = _shipped_config()
    ref = clips.build_groups(config)[0]["clips"][0]["ref"]
    _assert(ref["mode"] == "",
            "a patch entry's empty mode stays empty in the ref, so renaming "
            "the type's first mode never strands a favourite")


def test_scene_ref_survives_a_bank_reorder():
    print("test_scene_ref_survives_a_bank_reorder")
    config = _shipped_config()
    groups = clips.build_groups(config)
    scene = next(c for c in _all_clips(groups)
                 if c["type"] == "scene" and c["ref"]["name"] == "Relleno")

    # The Scenes tab reorders in the data, so the index moves under a favourite.
    moved = dict(scene["ref"], index=scene["ref"]["index"] + 7)
    _assert(clips.resolve_in(groups, [moved])[0] is scene,
            "a moved scene still resolves by name rather than firing its "
            "neighbour's look")

    renamed = dict(scene["ref"], name="Relleno (old)")
    _assert(clips.resolve_in(groups, [renamed])[0] is None,
            "a renamed scene goes unresolved — visible, not silently wrong")


def test_unresolvable_refs():
    print("test_unresolvable_refs")
    config = _shipped_config()
    groups = clips.build_groups(config)

    absent = clips.note_ref("no-such-fixture", "", 73, 127)
    unknown = {"kind": "kaboom"}
    live = _all_clips(groups)[0]["ref"]
    out = clips.resolve_in(groups, [absent, live, unknown])
    _assert(out[0] is None and out[2] is None,
            "an unpatched fixture and an unknown kind resolve to None")
    _assert(out[1] is not None,
            "a live ref beside them still resolves")
    _assert(len(out) == 3,
            "resolution is positional — nothing is dropped, so the caller can "
            "dim the dead entries and keep the show's list intact")


def test_unpatching_only_dims():
    print("test_unpatching_only_dims")
    config = _shipped_config()
    ref = clips.build_groups(config)[0]["clips"][0]["ref"]

    kept = [f for f in config.fixtures if f.type != ref["fixture"]]
    config.fixtures = kept
    _assert(clips.resolve(config, ref) is None,
            "unpatching the fixture leaves its favourite unresolvable")

    config.fixtures = shipped_fixtures()
    _assert(clips.resolve(config, ref) is not None,
            "repatching it brings the favourite straight back")


def test_channel_follows_the_patch():
    print("test_channel_follows_the_patch")
    config = _shipped_config()
    ref = clips.build_groups(config)[0]["clips"][0]["ref"]

    # Re-address the rig: the ref names the type, so it re-resolves onto
    # whatever MIDI channel that type now sits on.
    for inst in config.fixtures:
        inst.channel += 5
    clip = clips.resolve(config, ref)
    _assert(clip is not None and clip["payload"]["channel"] == 5,
            "a repatched fixture's favourite follows it instead of firing at "
            "the old channel")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nAll clip-model tests passed.")


if __name__ == "__main__":
    main()


