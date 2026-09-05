"""
Characterization test: the fixture browser's Scenes tab and the sidecar writes
behind it.

The rules being pinned:

  * scenes stay in their BANKS — the list is sections, not a flat list, because
    note 36+n selects the n-th bank and 24+n the n-th scene inside it;
  * the tab opens on a blank DRAFT — nothing selected, faders at their channel
    defaults — and the one button reads "Create" there and "Save" on a scene
    picked from the list;
  * "Save as new" branches instead of overwriting: the edits become the next
    scene and the one they came from goes back the way it was written;
  * the Save button carries ONLY the scene on the faders: edits to the list itself
    (new, duplicate, delete, drag, bank rename) are written as they happen, and
    edits to a scene are settled — saved or put back — before anything can move
    the faders off it;
  * an edit round-trips the RAW sidecar JSON: keys this app doesn't model, and
    other modes' blocks, survive untouched;
  * which mode block the banks land in follows whoever already supplies them,
    then "*" for a one-mode fixture and the named mode for a multi-mode one;
  * a definition that can't be written where it came from (a shipped def in a
    frozen build) is FORKED into the user layer, keeping its key;
  * renaming a bank rewrites the chases naming it — a chase references a bank
    by name, so this is the one rename that can be made safe;
  * the preview is the real stage render engine: the renderer the stage would
    pick, one body per body_count, fed the scene's DMX values.

Run with:
    uv run python tests/test_fixture_scenes.py
"""

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import fixture_library
from core.fixture_library import clear_cache, load_fixture_defs, resolve_fixture

SCHEMA = ("https://raw.githubusercontent.com/OpenLightingProject/"
          "open-fixture-library/master/schemas/fixture.json")


def _doc(name, categories=("Color Changer",), modes=None):
    return {
        "$schema": SCHEMA,
        "name": name,
        "categories": list(categories),
        "meta": {"authors": ["test"], "createDate": "2026-01-01",
                 "lastModifyDate": "2026-01-02"},
        "availableChannels": {
            "brightness": {"capability": {"type": "Intensity"}},
            "red": {"capability": {"type": "ColorIntensity", "color": "Red"}},
            "green": {"capability": {"type": "ColorIntensity", "color": "Green"}},
            "blue": {"capability": {"type": "ColorIntensity", "color": "Blue"}},
        },
        "modes": modes or [{"name": "4ch", "shortName": "4c",
                            "channels": ["brightness", "red", "green", "blue"]}],
    }


TWO_MODES = _doc("Wash 12", modes=[
    {"name": "Basic", "shortName": "3ch", "channels": ["red", "green", "blue"]},
    {"name": "Extended", "shortName": "4ch",
     "channels": ["brightness", "red", "green", "blue"]},
])

# A sidecar carrying a key this app doesn't model, to prove a write preserves it.
SIDECAR = {
    "tdmx": 1,
    "notes": "hand-written; do not lose me",
    "modes": {
        "*": {
            "banks": {
                "FILLS": [
                    {"brightness": 255, "red": 255, "green": 0, "blue": 0,
                     "name": "Red wash", "song_parts": ["CHORUS"]},
                    {"brightness": 128, "red": 0, "green": 0, "blue": 255,
                     "name": "Blue wash"},
                ],
                "STROBES": [
                    {"brightness": 255, "red": 255, "green": 255, "blue": 255,
                     "name": "White"},
                ],
            },
            "chases": [{"banks": ["FILLS", "STROBES"], "scenes": [0, 0]}],
        }
    },
}


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


@contextmanager
def _answers(button, log=None):
    """Answer every confirmation with `button`, recording what was asked.

    The tab asks before discarding or deleting, and an unanswered modal would
    hang the run — so `_with_tree` makes an unexpected one an assertion, and a
    test that expects one wraps it in here.
    """
    from PyQt6.QtWidgets import QMessageBox
    original = QMessageBox.question

    def stub(_parent, _title, text, *args, **kwargs):
        if log is not None:
            log.append(text)
        return getattr(QMessageBox.StandardButton, button)

    QMessageBox.question = staticmethod(stub)
    try:
        yield
    finally:
        QMessageBox.question = original


def _with_tree(tree, body, user_tree=None):
    """Run `body(shipped_dir, user_dir)` with `tree` as the shipped fixtures."""
    from PyQt6.QtWidgets import QMessageBox
    orig_shipped = fixture_library.fixtures_dir_shipped
    orig_user = fixture_library.fixtures_dir_user
    orig_question = QMessageBox.question

    def unexpected(_parent, _title, text, *args, **kwargs):
        raise AssertionError(f"unexpected confirmation, which would hang: {text}")

    QMessageBox.question = staticmethod(unexpected)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            shipped, user = Path(tmp) / "shipped", Path(tmp) / "user"
            shipped.mkdir()
            user.mkdir()
            fixture_library.fixtures_dir_shipped = lambda: str(shipped)
            fixture_library.fixtures_dir_user = lambda: user
            clear_cache()
            for relative, document in tree.items():
                path = shipped / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(document))
            for relative, document in (user_tree or {}).items():
                path = user / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(document))
            load_fixture_defs(force_reload=True)
            body(shipped, user)
    finally:
        QMessageBox.question = orig_question
        fixture_library.fixtures_dir_shipped = orig_shipped
        fixture_library.fixtures_dir_user = orig_user
        clear_cache()


# --- the sidecar writer -------------------------------------------------------

def test_which_mode_block_banks_are_written_to():
    print("test_which_mode_block_banks_are_written_to")
    from core.fixture_sidecar import banks_mode_key

    _assert(banks_mode_key({}, "4ch", single_mode=True) == "*",
            "a one-mode fixture writes its banks to the shared block")
    _assert(banks_mode_key({}, "Extended", single_mode=False) == "Extended",
            "a multi-mode fixture writes to the named block — modes don't "
            "share a channel set")
    _assert(banks_mode_key({"modes": {"*": {"banks": {}}}}, "Extended",
                           single_mode=False) == "*",
            "but whoever already supplies the banks wins")
    _assert(banks_mode_key({"modes": {"Extended": {"banks": {}},
                                      "*": {"channels": {}}}},
                           "Extended", single_mode=False) == "Extended",
            "the named block wins over a '*' block that has no banks")


def test_an_edit_preserves_everything_else_in_the_sidecar():
    print("test_an_edit_preserves_everything_else_in_the_sidecar")
    from core.fixture_library import save_fixture_banks

    def body(shipped, _user):
        fdef = resolve_fixture("Wash 12")
        banks = {name: [dict(s) for s in scenes]
                 for name, scenes in fdef.banks.items()}
        banks["FILLS"][0]["green"] = 77

        path = save_fixture_banks("Wash 12", fdef.mode, banks)
        _assert(path == shipped / "wash-12.tdmx.json",
                "the sidecar is written beside the OFL document it belongs to")

        written = json.loads(path.read_text())
        _assert(written["notes"] == "hand-written; do not lose me",
                "a key this app doesn't model survives the write")
        block = written["modes"]["*"]
        _assert(block["banks"]["FILLS"][0]["green"] == 77, "the edit landed")
        _assert(block["banks"]["FILLS"][0]["song_parts"] == ["CHORUS"],
                "the scene's other keys survive")
        _assert(list(block["banks"]) == ["FILLS", "STROBES"],
                "bank order is preserved — it is what note 36+n addresses")
        _assert(block["chases"] == [{"banks": ["FILLS", "STROBES"],
                                     "scenes": [0, 0]}],
                "chases are left alone when no bank was renamed")

        _assert(resolve_fixture("Wash 12").banks["FILLS"][0]["green"] == 77,
                "and the registry reloads, so the edit is live at once")

    _with_tree({"wash-12.json": _doc("Wash 12"),
                "wash-12.tdmx.json": SIDECAR}, body)


def test_a_type_with_no_sidecar_gets_one():
    print("test_a_type_with_no_sidecar_gets_one")
    from core.fixture_library import save_fixture_banks

    def body(shipped, _user):
        fdef = resolve_fixture("ADJ/Wash 12")
        _assert(fdef.banks == {}, "a fixture straight from OFL has no banks")

        scene = {"brightness": 255, "red": 10, "green": 20, "blue": 30,
                 "name": "First"}
        path = save_fixture_banks("ADJ/Wash 12", fdef.mode, {"SCENES": [scene]})
        _assert(path == shipped / "ADJ" / "wash-12.tdmx.json",
                "the sidecar is created beside the document, in its own folder")
        written = json.loads(path.read_text())
        _assert(written["tdmx"] == 1, "with the format version stamped")
        _assert(written["modes"]["*"]["banks"]["SCENES"][0]["name"] == "First",
                "and the new bank inside")
        _assert(resolve_fixture("ADJ/Wash 12").banks["SCENES"][0]["red"] == 10,
                "the new bank is live on the next resolve")

    _with_tree({"ADJ/wash-12.json": _doc("Wash 12")}, body)


def test_a_read_only_definition_forks_into_the_user_layer():
    print("test_a_read_only_definition_forks_into_the_user_layer")
    from core.fixture_library import save_fixture_banks

    original = fixture_library._is_persistable

    def body(shipped, user):
        # What a frozen build sees: the shipped layer is a temp extraction dir
        # whose writes would vanish on exit.
        fixture_library._is_persistable = (
            lambda directory: shipped not in Path(directory).parents
            and Path(directory) != shipped)
        try:
            fdef = resolve_fixture("ADJ/Wash 12")
            path = save_fixture_banks("ADJ/Wash 12", fdef.mode,
                                      {"SCENES": [{"brightness": 255, "red": 1,
                                                   "green": 2, "blue": 3}]})
            _assert(path == user / "ADJ" / "wash-12.tdmx.json",
                    "the sidecar is written into the writable user layer")
            _assert((user / "ADJ" / "wash-12.json").exists(),
                    "the OFL document is copied along — a sidecar never pairs "
                    "across layers")
            _assert(not (shipped / "ADJ" / "wash-12.tdmx.json").exists(),
                    "and the shipped definition is left pristine")

            load_fixture_defs(force_reload=True)
            _assert("ADJ/Wash 12" in load_fixture_defs(),
                    "the fork keeps the same registry key")
            _assert(resolve_fixture("ADJ/Wash 12").banks["SCENES"][0]["red"] == 1,
                    "so the user layer simply overrides the shipped def")
        finally:
            fixture_library._is_persistable = original

    _with_tree({"ADJ/wash-12.json": _doc("Wash 12")}, body)


def test_a_multi_mode_fixture_writes_to_its_named_mode():
    print("test_a_multi_mode_fixture_writes_to_its_named_mode")
    from core.fixture_library import save_fixture_banks

    def body(shipped, _user):
        fdef = resolve_fixture("Wash 12", "Extended")
        _assert(fdef.mode == "Extended", "the mode resolved")
        save_fixture_banks("Wash 12", fdef.mode,
                           {"SCENES": [{"brightness": 5, "red": 5,
                                        "green": 5, "blue": 5}]})
        written = json.loads((shipped / "wash-12.tdmx.json").read_text())
        _assert(list(written["modes"]) == ["Extended"],
                "the banks land in the mode they were authored against")

    _with_tree({"wash-12.json": TWO_MODES}, body)


# --- the tab ------------------------------------------------------------------

def _tab():
    from ui.fixture.scenes_tab import FixtureScenesTab
    return FixtureScenesTab()


def _select(tab, bank, index=-1):
    """Put the tree cursor on a bank header (index -1) or one of its scenes."""
    from ui.fixture.scenes_tab import _ROLE_BANK
    for row in range(tab.tree.topLevelItemCount()):
        item = tab.tree.topLevelItem(row)
        if item.data(0, _ROLE_BANK) == bank:
            tab.tree.setCurrentItem(item if index < 0 else item.child(index))
            return
    raise AssertionError(f"no bank '{bank}' in the tree")


def test_the_list_keeps_banks_as_sections(qapp):
    print("test_the_list_keeps_banks_as_sections")

    def body(_shipped, _user):
        tab = _tab()
        fdef = resolve_fixture("Wash 12")
        tab.set_fixture("Wash 12", fdef, dmx_start=10)

        _assert(tab.tree.topLevelItemCount() == 2, "one row per bank")
        _assert([tab.tree.topLevelItem(i).text(0) for i in range(2)]
                == ["FILLS", "STROBES"], "in the order the sidecar declares")
        _assert(tab.tree.topLevelItem(0).childCount() == 2,
                "with that bank's scenes beneath it")
        _assert(tab._selected is None and tab._drafting(),
                "nothing starts selected — the tab opens on a blank draft")
        _assert(tab.name_input.text() == "" and tab.name_input.isEnabled(),
                "with an unnamed, editable scene in the editor")

        _assert(len(tab._faders) == 4, "one fader per channel of the mode")
        _assert([f.address_label.text() for f in tab._faders]
                == ["10", "11", "12", "13"],
                "addressed from the DMX start the dialog is offering")
        _assert(all(f.isEnabled() for f in tab._faders),
                "the faders are live from the first click, no scene needed")

        _select(tab, "FILLS", 0)
        _assert(tab.name_input.text() == "Red wash",
                "picking a scene from the list loads it into the editor")
        _assert([f.slider.value() for f in tab._faders] == [255, 255, 0, 0],
                "with the scene's values on the faders")
        _assert(tab._part_chips["CHORUS"].isChecked(),
                "the scene's song_parts are shown as tags")
        _assert(not tab._part_chips["INTRO"].isChecked(), "and only those")

        tab.set_dmx_start(100)
        _assert(tab._faders[0].address_label.text() == "100",
                "moving the patch re-stamps the fader addresses")

    _with_tree({"wash-12.json": _doc("Wash 12"),
                "wash-12.tdmx.json": SIDECAR}, body)


def test_the_preview_is_the_stage_render_engine(qapp):
    print("test_the_preview_is_the_stage_render_engine")
    from stage.registry import renderer_for
    from stage.metrics import body_count

    def body(_shipped, _user):
        tab = _tab()
        fdef = resolve_fixture("Wash 12")
        tab.set_fixture("Wash 12", fdef, dmx_start=1)

        expected = renderer_for(fdef)
        _assert(len(tab.preview._bodies) == body_count(fdef),
                "one preview body per body the stage would draw")
        _assert(all(isinstance(b, expected) for b in tab.preview._bodies),
                f"drawn by the stage's own renderer ({expected.__name__})")

        _select(tab, "FILLS", 0)
        # "Red wash": full brightness, red only.
        _assert(tab.preview._bodies[0].state.scaled_rgb() == (255, 0, 0),
                "the body resolves the selected scene's DMX")

        tab._faders[1].slider.setValue(0)
        tab._faders[2].slider.setValue(255)
        _assert(tab.preview._bodies[0].state.scaled_rgb() == (0, 255, 0),
                "and follows the faders live")
        _assert("100 %" in tab.summary_label.text(),
                "the read-out follows too")

    _with_tree({"wash-12.json": _doc("Wash 12"),
                "wash-12.tdmx.json": SIDECAR}, body)


def test_the_readout_follows_a_switching_channel(qapp):
    print("test_the_readout_follows_a_switching_channel")
    from ui.fixture.scenes_tab import _capability_rows

    # Slot 3 is a Pan while "Mode" is low and a Tilt while it is high. Read
    # channel-by-channel it would always claim to be a Pan.
    doc = _doc("Switcher")
    doc["availableChannels"] = {
        "brightness": {"capability": {"type": "Intensity"}},
        "Mode": {"capabilities": [
            {"dmxRange": [0, 127], "type": "Maintenance",
             "switchChannels": {"Pan / Tilt": "Pan"}},
            {"dmxRange": [128, 255], "type": "Maintenance",
             "switchChannels": {"Pan / Tilt": "Tilt"}}]},
        "Pan": {"capability": {"type": "Pan", "angleStart": "0deg",
                               "angleEnd": "180deg"}},
        "Tilt": {"capability": {"type": "Tilt", "angleStart": "0deg",
                                "angleEnd": "90deg"}},
    }
    doc["modes"] = [{"name": "3ch", "channels": ["brightness", "Mode", "Pan / Tilt"]}]

    def body(_shipped, _user):
        fdef = resolve_fixture("Switcher")
        low = dict(_capability_rows(fdef, [255, 0, 255]))
        _assert(low.get("pan") == "180°" and "tilt" not in low,
                "trigger low -> the shared slot reads out as pan")
        high = dict(_capability_rows(fdef, [255, 200, 255]))
        _assert(high.get("tilt") == "90°" and "pan" not in high,
                "trigger high -> the same slot reads out as tilt")

    _with_tree({"switcher.json": doc}, body)


def test_the_faders_compose_a_new_scene_and_create_writes_it(qapp):
    print("test_the_faders_compose_a_new_scene_and_create_writes_it")

    def sidecar(shipped):
        return json.loads((shipped / "wash-12.tdmx.json").read_text())

    def body(shipped, _user):
        tab = _tab()
        tab.set_fixture("Wash 12", resolve_fixture("Wash 12"), dmx_start=1)
        _assert(tab.save_button.text() == "Create" and tab.save_button.isEnabled(),
                "the tab opens offering to create what the faders hold")
        _assert("FILLS" in tab.status_label.text(),
                "saying which bank it would land in")

        # The whole point: walk in, move faders, press the button.
        for fader, value in zip(tab._faders, (200, 10, 20, 30)):
            fader.slider.setValue(value)
        _assert(len(sidecar(shipped)["modes"]["*"]["banks"]["FILLS"]) == 2,
                "nothing is written while the draft is being dialled in")
        tab.name_input.setText("Sunrise")
        tab._on_name_edited()
        tab.save_button.click()

        scenes = sidecar(shipped)["modes"]["*"]["banks"]["FILLS"]
        _assert(len(scenes) == 3, "Create appends the draft to the target bank")
        _assert(scenes[2] == {"brightness": 200, "red": 10, "green": 20,
                              "blue": 30, "name": "Sunrise"},
                "with the fader values and the typed name, and every channel of "
                "the mode so the engine accepts it")
        _assert(tab._selected == ("FILLS", 2),
                "and the new scene is what the editor is now on")
        _assert(tab.save_button.text() == "Save" and not tab.save_button.isEnabled(),
                "so the button becomes an ordinary, idle Save")

        # "New scene" creates one on the spot rather than arming a draft.
        tab._new_scene()
        _assert(not tab._drafting() and tab._selected == ("FILLS", 3),
                "+ New scene puts a real scene in the target bank at once")
        _assert(tab.tree.topLevelItem(0).childCount() == 4,
                "so the list shows it without waiting for a button")
        _assert(tab.name_input.text() == "Scene 4"
                and tab.focusWidget() is tab.name_input
                and tab.name_input.hasSelectedText(),
                "named for its slot, with that name focused and selected to "
                "be typed over")
        _assert([s["name"] for s in sidecar(shipped)["modes"]["*"]["banks"]["FILLS"]]
                == ["Red wash", "Blue wash", "Sunrise", "Scene 4"],
                "and the sidecar has it: adding to the list is not what the "
                "Save button is for")
        _assert(tab.save_button.text() == "Save" and not tab.save_button.isEnabled(),
                "so the button is idle — the new scene has no edits yet")
        tab._faders[0].slider.setValue(5)
        _assert(tab.save_button.isEnabled(), "dialling it in is what wakes it")
        tab.save_button.click()
        _assert(sidecar(shipped)["modes"]["*"]["banks"]["FILLS"][3]
                == {"brightness": 5, "red": 0, "green": 0, "blue": 0,
                    "name": "Scene 4"},
                "and Save writes it with whatever was dialled in")

        # Modifying an existing scene overwrites it instead.
        from PyQt6.QtTest import QTest
        _select(tab, "FILLS", 0)
        tab.name_input.selectAll()
        QTest.keyClicks(tab.name_input, "C")
        _assert(tab.save_button.isEnabled(),
                "the button wakes on the first character typed, not on the "
                "name field losing focus")
        QTest.keyClicks(tab.name_input, "rimson")
        _assert(tab.tree.topLevelItem(0).child(0).text(0) == "Crimson",
                "and the row keeps up with the typing")
        tab._faders[0].slider.setValue(64)
        _assert(sidecar(shipped)["modes"]["*"]["banks"]["FILLS"][0]["name"]
                == "Red wash",
                "edits to it stay in memory until the button is pressed")
        _assert("Crimson" in tab.status_label.text(),
                "and the status line names the scene they belong to")
        _assert(tab.save_button.isEnabled() and tab.save_button.text() == "Save",
                "which now offers to update that scene, not add one")
        tab.save_button.click()
        scenes = sidecar(shipped)["modes"]["*"]["banks"]["FILLS"]
        _assert(len(scenes) == 4, "Save adds nothing to the bank")
        _assert(scenes[0]["name"] == "Crimson" and scenes[0]["brightness"] == 64,
                "it overwrites the scene that was picked")
        _assert(not tab.save_button.isEnabled(), "and the button goes quiet again")

        tab._part_chips["SOLO"].setChecked(True)
        tab._on_song_parts_changed()
        tab.save_button.click()
        _assert(sidecar(shipped)["modes"]["*"]["banks"]["FILLS"][0]["song_parts"]
                == ["CHORUS", "SOLO"],
                "song-part tags are stored in their canonical order")

        _select(tab, "FILLS", 0)
        tab._duplicate()
        scenes = sidecar(shipped)["modes"]["*"]["banks"]["FILLS"]
        _assert(len(scenes) == 5 and scenes[1]["name"] == "Crimson copy",
                "Duplicate inserts the copy right after its original, written")
        _assert(not tab.save_button.isEnabled(),
                "with nothing left owing on the button")

        _select(tab, "FILLS", 1)
        asked = []
        with _answers("Yes", asked):
            tab._delete()
        _assert(len(asked) == 1 and "Crimson copy" in asked[0],
                "Delete confirms by name — it is a write, not a pending edit")
        _assert([s["name"] for s in sidecar(shipped)["modes"]["*"]["banks"]["FILLS"]]
                == ["Crimson", "Blue wash", "Sunrise", "Scene 4"],
                "and removes just that scene, leaving the rest in order")

        # Stepping off a scene onto a bank header starts a fresh scene there.
        _select(tab, "STROBES")
        _assert(tab._drafting() and all(f.isEnabled() for f in tab._faders),
                "a bank header is a blank draft, faders live")
        _assert(tab._target_bank() == "STROBES",
                "aimed at the bank that was clicked")
        tab._faders[0].slider.setValue(7)
        _select(tab, "FILLS")
        _assert(tab._drafting() and tab._faders[0].slider.value() == 7,
                "and moving between bank headers only re-targets it, keeping "
                "what has been dialled in")
        _assert(tab._target_bank() == "FILLS", "now aimed at the other bank")

    _with_tree({"wash-12.json": _doc("Wash 12"),
                "wash-12.tdmx.json": SIDECAR}, body)


def test_save_as_new_branches_the_scene_it_was_edited_from(qapp):
    print("test_save_as_new_branches_the_scene_it_was_edited_from")

    def banks(shipped):
        return json.loads((shipped / "wash-12.tdmx.json").read_text()
                          )["modes"]["*"]["banks"]

    def body(shipped, _user):
        tab = _tab()
        tab.set_fixture("Wash 12", resolve_fixture("Wash 12"), dmx_start=1)
        _assert(not tab.save_as_button.isEnabled(),
                "a draft has nothing to branch from — Create is that")

        _select(tab, "FILLS", 0)
        _assert(tab.save_as_button.isEnabled(),
                "a scene picked from the list does")
        tab._faders[1].slider.setValue(0)
        tab._faders[2].slider.setValue(255)
        tab.name_input.setText("Green wash")
        tab._on_name_edited()
        tab.save_as_button.click()

        scenes = banks(shipped)["FILLS"]
        _assert([s["name"] for s in scenes]
                == ["Red wash", "Green wash", "Blue wash"],
                "the edits land as a new scene right after their source")
        _assert(scenes[0] == {"brightness": 255, "red": 255, "green": 0,
                              "blue": 0, "name": "Red wash",
                              "song_parts": ["CHORUS"]},
                "which is left exactly as it was written")
        _assert(scenes[1]["red"] == 0 and scenes[1]["green"] == 255
                and scenes[1]["song_parts"] == ["CHORUS"],
                "and the new one carries the changes, over the rest of the "
                "scene it came from")
        _assert(tab._selected == ("FILLS", 1)
                and tab.focusWidget() is tab.name_input,
                "the editor moves onto the new scene, name ready to be typed")
        _assert(not tab.save_button.isEnabled(),
                "with nothing owing — it was written, not left pending")

        # Round two comes off the scene just made, so a run of them comes out
        # in the order it was dialled.
        tab._faders[0].slider.setValue(12)
        tab.save_as_button.click()
        _assert([s["name"] for s in banks(shipped)["FILLS"]]
                == ["Red wash", "Green wash", "Green wash copy", "Blue wash"],
                "an unrenamed branch is named for its source, as Duplicate is")

    _with_tree({"wash-12.json": _doc("Wash 12"),
                "wash-12.tdmx.json": SIDECAR}, body)


def test_leaving_an_edited_scene_settles_it(qapp):
    print("test_leaving_an_edited_scene_settles_it")

    def sidecar(shipped):
        return json.loads((shipped / "wash-12.tdmx.json").read_text())

    def body(shipped, _user):
        tab = _tab()
        tab.set_fixture("Wash 12", resolve_fixture("Wash 12"), dmx_start=1)
        row = tab.tree.topLevelItem(0).child(0)

        # Declining puts the scene back the way the sidecar has it.
        _select(tab, "FILLS", 0)
        tab.name_input.setText("Scarlet")
        tab._on_name_edited()
        tab._faders[0].slider.setValue(9)
        _assert(row.text(0) == "Scarlet",
                "the row follows the edit while it is pending")
        asked = []
        with _answers("No", asked):
            _select(tab, "FILLS", 1)
        _assert(len(asked) == 1 and "Scarlet" in asked[0],
                "stepping onto another scene asks about the one being left, "
                "named as the editor and the row are showing it")
        _assert(sidecar(shipped)["modes"]["*"]["banks"]["FILLS"][0]
                == {"brightness": 255, "red": 255, "green": 0, "blue": 0,
                    "name": "Red wash", "song_parts": ["CHORUS"]},
                "declining writes nothing")
        _assert(row.text(0) == "Red wash",
                "and puts the row back with it")
        _select(tab, "FILLS", 0)      # nothing pending now, so nothing is asked
        _assert(tab.name_input.text() == "Red wash"
                and [f.slider.value() for f in tab._faders] == [255, 255, 0, 0],
                "the scene itself is back the way it was written")

        # Accepting writes the edit before the faders move on.
        tab._faders[1].slider.setValue(3)
        _assert(tab.save_button.isEnabled(),
                "the button carries the scene on the faders")
        with _answers("Yes"):
            _select(tab, "FILLS", 1)
        _assert(sidecar(shipped)["modes"]["*"]["banks"]["FILLS"][0]["red"] == 3,
                "and accepting is the same write the button would have made")
        _assert(not tab.save_button.isEnabled(),
                "which leaves it carrying the newly picked scene, owing nothing")

    _with_tree({"wash-12.json": _doc("Wash 12"),
                "wash-12.tdmx.json": SIDECAR}, body)


def test_renaming_a_bank_follows_through_into_its_chases(qapp):
    print("test_renaming_a_bank_follows_through_into_its_chases")

    def body(shipped, _user):
        from PyQt6.QtCore import QCoreApplication
        from ui.fixture.scenes_tab import _ROLE_BANK

        tab = _tab()
        tab.set_fixture("Wash 12", resolve_fixture("Wash 12"), dmx_start=1)

        # Drive the real inline edit: the rename runs deferred, because
        # relaying the tree would delete the item mid-itemChanged.
        item = tab.tree.topLevelItem(0)
        _assert(item.data(0, _ROLE_BANK) == "FILLS", "the first bank is FILLS")
        item.setText(0, "WASHES")
        QCoreApplication.processEvents()

        written = json.loads((shipped / "wash-12.tdmx.json").read_text())
        banks = written["modes"]["*"]["banks"]
        _assert(list(banks) == ["WASHES", "STROBES"],
                "the bank is renamed in place, keeping its position")
        _assert(written["modes"]["*"]["chases"][0]["banks"]
                == ["WASHES", "STROBES"],
                "and every chase naming it is rewritten to follow")

    _with_tree({"wash-12.json": _doc("Wash 12"),
                "wash-12.tdmx.json": SIDECAR}, body)


def test_a_drag_reorders_scenes_and_banks(qapp):
    print("test_a_drag_reorders_scenes_and_banks")

    def body(shipped, _user):
        tab = _tab()
        tab.set_fixture("Wash 12", resolve_fixture("Wash 12"), dmx_start=1)

        def saved_banks():
            # A drag is a change to the list, so it is on disk already.
            return json.loads(
                (shipped / "wash-12.tdmx.json").read_text())["modes"]["*"]["banks"]

        # Move FILLS' second scene to the front of the bank.
        tab._on_move("FILLS", 1, "FILLS", 0)
        banks = saved_banks()
        _assert([s["name"] for s in banks["FILLS"]] == ["Blue wash", "Red wash"],
                "a scene drag reorders within its bank")

        # Move it across into STROBES.
        tab._on_move("FILLS", 0, "STROBES", 1)
        banks = saved_banks()
        _assert([s["name"] for s in banks["FILLS"]] == ["Red wash"],
                "a cross-bank drag leaves the source bank")
        _assert([s["name"] for s in banks["STROBES"]] == ["White", "Blue wash"],
                "and lands at the drop position in the target bank")

        # Drag STROBES above FILLS (index -1 marks the bank itself).
        tab._on_move("STROBES", -1, "", 0)
        banks = saved_banks()
        _assert(list(banks) == ["STROBES", "FILLS"],
                "a bank drag reorders the banks — what note 36+n addresses")
        _assert(sum(len(scenes) for scenes in banks.values()) == 3,
                "and the open draft is not swept in as a scene")

    _with_tree({"wash-12.json": _doc("Wash 12"),
                "wash-12.tdmx.json": SIDECAR}, body)


def test_the_dialog_shows_the_tab_and_follows_its_selection(qapp):
    print("test_the_dialog_shows_the_tab_and_follows_its_selection")
    from core.config import Config
    from ui.fixture.browser import AddFixtureDialog
    from stage.controller import StageController

    def body(_shipped, _user):
        config = Config()
        config.fixtures = []
        dialog = AddFixtureDialog(StageController(config))
        _assert([dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
                == ["General", "Channels", "Modes", "Scenes", "Chases"],
                "Scenes joins the detail panel's tabs, with Chases after it")

        dialog.select_type("Wash 12")
        _assert(list(dialog.scenes_tab._banks) == ["FILLS", "STROBES"],
                "picking a type loads its banks")
        _assert(len(dialog.scenes_tab._faders) == 4,
                "and its channels, at the mode on screen")

        dialog.dmx_spin.setValue(200)
        _assert(dialog.scenes_tab._faders[0].address_label.text() == "200",
                "the faders follow the patch address the footer is offering")

        dialog.select_type("Two Modes")
        dialog.set_mode("Basic")
        _assert(len(dialog.scenes_tab._faders) == 3,
                "switching mode relays the faders — a scene is per channel set")

    _with_tree({"wash-12.json": _doc("Wash 12"),
                "wash-12.tdmx.json": SIDECAR,
                "two-modes.json": dict(TWO_MODES, name="Two Modes")}, body)


def test_every_sidecar_write_announces_itself(qapp):
    print("test_every_sidecar_write_announces_itself")
    from PyQt6.QtWidgets import QDialog, QMessageBox
    from core.config import Config
    from ui.fixture.browser import AddFixtureDialog
    from stage.controller import StageController

    def body(_shipped, _user):
        config = Config()
        config.fixtures = []
        dialog = AddFixtureDialog(StageController(config))
        writes = []
        dialog.library_changed.connect(lambda: writes.append(True))
        dialog.select_type("Wash 12")
        tab = dialog.scenes_tab

        tab._faders[0].slider.setValue(120)
        _assert(writes == [], "dialling in a draft writes nothing, announces "
                              "nothing")
        tab.save_button.click()
        _assert(len(writes) == 1, "Create writes the sidecar and says so")

        _select(tab, "FILLS", 0)
        tab._faders[0].slider.setValue(64)
        tab.save_button.click()
        _assert(len(writes) == 2, "so does Save on an edited scene")

        _select(tab, "FILLS", 0)
        with _answers("Yes"):
            tab._delete()
        _assert(len(writes) == 3, "a delete writes as it happens, and says so")

        # The whole point: the Scenes tab writes the LIBRARY, so Cancel settles
        # its pending work exactly as OK does — and must announce it too.
        _select(tab, "FILLS", 0)
        tab._faders[0].slider.setValue(32)
        original = QMessageBox.question
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Yes)
        try:
            dialog.done(QDialog.DialogCode.Rejected)
        finally:
            QMessageBox.question = original
        _assert(len(writes) == 4,
                "closing on Cancel saves the pending scene edit and announces "
                "the write — the clip library reads those banks")

    _with_tree({"wash-12.json": _doc("Wash 12"),
                "wash-12.tdmx.json": SIDECAR}, body)


def main():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    test_which_mode_block_banks_are_written_to()
    test_an_edit_preserves_everything_else_in_the_sidecar()
    test_a_type_with_no_sidecar_gets_one()
    test_a_read_only_definition_forks_into_the_user_layer()
    test_a_multi_mode_fixture_writes_to_its_named_mode()
    test_the_list_keeps_banks_as_sections(app)
    test_the_preview_is_the_stage_render_engine(app)
    test_the_readout_follows_a_switching_channel(app)
    test_the_faders_compose_a_new_scene_and_create_writes_it(app)
    test_save_as_new_branches_the_scene_it_was_edited_from(app)
    test_leaving_an_edited_scene_settles_it(app)
    test_renaming_a_bank_follows_through_into_its_chases(app)
    test_a_drag_reorders_scenes_and_banks(app)
    test_the_dialog_shows_the_tab_and_follows_its_selection(app)
    test_every_sidecar_write_announces_itself(app)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
