"""
Characterization test: the fixture browser's Scenes tab and the sidecar writes
behind it.

The rules being pinned:

  * scenes stay in their BANKS — the list is sections, not a flat list, because
    note 36+n selects the n-th bank and 24+n the n-th scene inside it;
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


def _with_tree(tree, body, user_tree=None):
    """Run `body(shipped_dir, user_dir)` with `tree` as the shipped fixtures."""
    orig_shipped = fixture_library.fixtures_dir_shipped
    orig_user = fixture_library.fixtures_dir_user
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
    from ui.fixture_scenes_tab import FixtureScenesTab
    return FixtureScenesTab()


def _select(tab, bank, index=-1):
    """Put the tree cursor on a bank header (index -1) or one of its scenes."""
    from ui.fixture_scenes_tab import _ROLE_BANK
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
        _assert(tab._selected == ("FILLS", 0), "the first scene starts selected")
        _assert(tab.name_input.text() == "Red wash", "and is loaded into the editor")

        _assert(len(tab._faders) == 4, "one fader per channel of the mode")
        _assert([f.address_label.text() for f in tab._faders]
                == ["10", "11", "12", "13"],
                "addressed from the DMX start the dialog is offering")
        _assert([f.slider.value() for f in tab._faders] == [255, 255, 0, 0],
                "loaded with the scene's values")
        _assert(tab._part_buttons["CHORUS"].isChecked(),
                "the scene's song_parts are shown as tags")
        _assert(not tab._part_buttons["INTRO"].isChecked(), "and only those")

        tab.set_dmx_start(100)
        _assert(tab._faders[0].address_label.text() == "100",
                "moving the patch re-stamps the fader addresses")

    _with_tree({"wash-12.json": _doc("Wash 12"),
                "wash-12.tdmx.json": SIDECAR}, body)


def test_the_preview_is_the_stage_render_engine(qapp):
    print("test_the_preview_is_the_stage_render_engine")
    from ui.stage.registry import renderer_for
    from ui.stage.base import body_count

    def body(_shipped, _user):
        tab = _tab()
        fdef = resolve_fixture("Wash 12")
        tab.set_fixture("Wash 12", fdef, dmx_start=1)

        expected = renderer_for(fdef)
        _assert(len(tab.preview._bodies) == body_count(fdef),
                "one preview body per body the stage would draw")
        _assert(all(isinstance(b, expected) for b in tab.preview._bodies),
                f"drawn by the stage's own renderer ({expected.__name__})")

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
    from ui.fixture_scenes_tab import _capability_rows

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


def test_editing_writes_through_to_the_sidecar(qapp):
    print("test_editing_writes_through_to_the_sidecar")

    def sidecar(shipped):
        return json.loads((shipped / "wash-12.tdmx.json").read_text())

    def body(shipped, _user):
        tab = _tab()
        tab.set_fixture("Wash 12", resolve_fixture("Wash 12"), dmx_start=1)

        tab.name_input.setText("Crimson")
        tab._on_name_edited()
        _assert(sidecar(shipped)["modes"]["*"]["banks"]["FILLS"][0]["name"]
                == "Crimson", "a rename is saved as it is committed")

        tab._faders[0].slider.setValue(64)
        tab.flush()                       # the fader write is debounced
        _assert(sidecar(shipped)["modes"]["*"]["banks"]["FILLS"][0]["brightness"]
                == 64, "so is a fader move, once it settles")

        tab._part_buttons["SOLO"].setChecked(True)
        tab._on_song_parts_changed()
        _assert(sidecar(shipped)["modes"]["*"]["banks"]["FILLS"][0]["song_parts"]
                == ["CHORUS", "SOLO"],
                "song-part tags are stored in their canonical order")

        tab._add_scene()
        scenes = sidecar(shipped)["modes"]["*"]["banks"]["FILLS"]
        _assert(len(scenes) == 3, "+ New appends to the selected bank")
        _assert(scenes[2]["name"] == "Scene 3", "with a placeholder name")
        _assert(set(scenes[2]) == {"brightness", "red", "green", "blue", "name"},
                "and every channel of the mode, so the engine accepts it")

        _select(tab, "FILLS", 0)
        tab._duplicate()
        scenes = sidecar(shipped)["modes"]["*"]["banks"]["FILLS"]
        _assert(len(scenes) == 4 and scenes[1]["name"] == "Crimson copy",
                "Duplicate inserts the copy right after its original")

        _select(tab, "FILLS", 1)
        tab._delete()
        _assert(len(sidecar(shipped)["modes"]["*"]["banks"]["FILLS"]) == 3,
                "Delete removes just that scene")
        _assert([s["name"] for s in sidecar(shipped)["modes"]["*"]["banks"]["FILLS"]]
                == ["Crimson", "Blue wash", "Scene 3"],
                "and leaves the rest in order")

    _with_tree({"wash-12.json": _doc("Wash 12"),
                "wash-12.tdmx.json": SIDECAR}, body)


def test_renaming_a_bank_follows_through_into_its_chases(qapp):
    print("test_renaming_a_bank_follows_through_into_its_chases")

    def body(shipped, _user):
        from PyQt6.QtCore import QCoreApplication
        from ui.fixture_scenes_tab import _ROLE_BANK

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

        # Move FILLS' second scene to the front of the bank.
        tab._on_move("FILLS", 1, "FILLS", 0)
        banks = json.loads((shipped / "wash-12.tdmx.json").read_text())["modes"]["*"]["banks"]
        _assert([s["name"] for s in banks["FILLS"]] == ["Blue wash", "Red wash"],
                "a scene drag reorders within its bank")

        # Move it across into STROBES.
        tab._on_move("FILLS", 0, "STROBES", 1)
        banks = json.loads((shipped / "wash-12.tdmx.json").read_text())["modes"]["*"]["banks"]
        _assert([s["name"] for s in banks["FILLS"]] == ["Red wash"],
                "a cross-bank drag leaves the source bank")
        _assert([s["name"] for s in banks["STROBES"]] == ["White", "Blue wash"],
                "and lands at the drop position in the target bank")

        # Drag STROBES above FILLS (index -1 marks the bank itself).
        tab._on_move("STROBES", -1, "", 0)
        banks = json.loads((shipped / "wash-12.tdmx.json").read_text())["modes"]["*"]["banks"]
        _assert(list(banks) == ["STROBES", "FILLS"],
                "a bank drag reorders the banks — what note 36+n addresses")

    _with_tree({"wash-12.json": _doc("Wash 12"),
                "wash-12.tdmx.json": SIDECAR}, body)


def test_the_dialog_shows_the_tab_and_follows_its_selection(qapp):
    print("test_the_dialog_shows_the_tab_and_follows_its_selection")
    from core.config import Config
    from ui.add_fixture_dialog import AddFixtureDialog
    from ui.stage_controller import StageController

    def body(_shipped, _user):
        config = Config()
        config.fixtures = []
        dialog = AddFixtureDialog(StageController(config))
        _assert([dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
                == ["General", "Channels", "Modes", "Scenes"],
                "Scenes joins the detail panel's tabs")

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
    test_editing_writes_through_to_the_sidecar(app)
    test_renaming_a_bank_follows_through_into_its_chases(app)
    test_a_drag_reorders_scenes_and_banks(app)
    test_the_dialog_shows_the_tab_and_follows_its_selection(app)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
