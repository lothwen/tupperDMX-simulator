"""
Characterization test: the fixture library's manufacturer layer + the Add
fixture browser built on it.

The rules being pinned:

  * a subfolder of a fixtures dir is a MANUFACTURER, and it namespaces the
    registry key — so two makers can ship a fixture of the same name without
    one silently replacing the other;
  * a file directly in a fixtures dir keeps its bare key, because every saved
    patch (and the shipped ledbar/blinder) refers to it that way;
  * a sidecar only pairs with the OFL document beside it, not with a same-named
    document in another manufacturer's folder;
  * the browser groups by manufacturer OR by category (a fixture with several
    categories appears under each), and search spans all three fields;
  * the footer refuses a patch that overruns the universe, overlaps a patched
    fixture, or reuses a MIDI channel — OK stays disabled rather than letting
    a broken patch through.

Run with:
    uv run python tests/test_add_fixture_dialog.py
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


def _doc(name, categories=("Color Changer",), modes=None, **extra):
    document = {
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
    document.update(extra)
    return document


# A fully-populated document: everything the General tab can show.
RICH = _doc(
    "Focus Spot 4Z",
    categories=("Moving Head", "Color Changer"),
    shortName="FS4",
    comment="Imported from the Open Fixture Library.",
    links={"manual": ["https://example.test/manual.pdf"],
           "productPage": ["https://example.test/product"]},
    rdm={"modelId": 4242, "softwareVersion": "2.4"},
    physical={
        "dimensions": [346, 346, 266], "weight": 8.6, "power": 600,
        "powerConnectors": {"IEC C13": "input and output"},
        "DMXconnector": "3-pin and 5-pin",
        "bulb": {"type": "LED engine", "colorTemperature": 3400, "lumens": 3000},
        "lens": {"name": "PC", "degreesMinMax": [8, 42]},
    },
    modes=[{"name": "Basic", "shortName": "3ch",
            "channels": ["red", "green", "blue"]},
           {"name": "Extended", "shortName": "4ch",
            "channels": ["brightness", "red", "green", "blue"]}],
)
RICH["meta"]["importPlugin"] = {"plugin": "qlcplus_4.12.2", "date": "2022-05-14"}


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _with_tree(tree, body):
    """Run `body()` with `tree` — {relative path: document} — as the only
    shipped fixture definitions."""
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
            load_fixture_defs(force_reload=True)
            body()
    finally:
        fixture_library.fixtures_dir_shipped = orig_shipped
        fixture_library.fixtures_dir_user = orig_user
        clear_cache()


# --- the library layer --------------------------------------------------------

def test_subfolders_namespace_the_registry_key():
    print("test_subfolders_namespace_the_registry_key")

    def body():
        defs = load_fixture_defs()
        _assert("ADJ/Wash 12" in defs, "a fixture in ADJ/ keys under its folder")
        _assert("Cameo/Wash 12" in defs,
                "the same name under another maker is a separate type")
        _assert("loose" in defs, "a root-level file keeps its bare key")
        _assert(defs["ADJ/Wash 12"].manufacturer == "ADJ",
                "the def records which folder it came from")
        _assert(defs["loose"].manufacturer == "",
                "a root-level def has no manufacturer")
        _assert(resolve_fixture("Cameo/Wash 12") is not None,
                "a namespaced key resolves like any other")

    _with_tree({
        "ADJ/wash-12.json": _doc("Wash 12"),
        "Cameo/wash-12.json": _doc("Wash 12"),
        "loose.json": _doc("loose"),
    }, body)


def test_a_sidecar_only_pairs_within_its_own_folder():
    print("test_a_sidecar_only_pairs_within_its_own_folder")
    sidecar = {"tdmx": 1, "fixture": "wash-12.json",
               "modes": {"*": {"banks": {"Reds": [{"red": 255}]}}}}

    def body():
        defs = load_fixture_defs()
        _assert(list(defs["ADJ/Wash 12"].banks) == ["Reds"],
                "the sidecar applies to the document beside it")
        _assert(defs["Cameo/Wash 12"].banks == {},
                "and not to the same-named document in another folder")

    _with_tree({
        "ADJ/wash-12.json": _doc("Wash 12"),
        "ADJ/wash-12.tdmx.json": sidecar,
        "Cameo/wash-12.json": _doc("Wash 12"),
    }, body)


def test_ofl_document_metadata_is_parsed():
    print("test_ofl_document_metadata_is_parsed")
    from core.fixture_library import fixture_document

    def body():
        document = fixture_document("ADJ/Focus Spot 4Z")
        _assert(document.short_name == "FS4", "shortName survives")
        _assert(document.meta.authors == ("test",), "authors survive")
        _assert(document.meta.last_modify_date == "2026-01-02", "dates survive")
        _assert(document.meta.import_plugin.plugin == "qlcplus_4.12.2",
                "the import plugin survives")
        _assert(document.comment.startswith("Imported"), "the comment survives")
        _assert(document.links["manual"] == ("https://example.test/manual.pdf",),
                "links survive as a kind -> urls map")
        _assert(document.rdm.model_id == 4242, "RDM data survives")
        physical = document.physical
        _assert(physical.power_connectors == {"IEC C13": "input and output"},
                "power connectors survive")
        _assert(physical.dmx_connector == "3-pin and 5-pin", "the DMX connector survives")
        _assert(physical.bulb_lumens == 3000, "bulb lumens survive")

    _with_tree({"ADJ/focus-spot-4z.json": RICH}, body)


# --- the dialog ---------------------------------------------------------------

def _dialog(qapp=None):
    from core.config import Config
    from stage.controller import StageController
    from ui.fixture.browser import AddFixtureDialog
    config = Config()
    config.fixtures = []
    return AddFixtureDialog(StageController(config))


def test_browser_groups_by_manufacturer_and_by_category(qapp):
    print("test_browser_groups_by_manufacturer_and_by_category")
    from ui.fixture.browser import GROUP_BY_CATEGORY

    def groups(dialog):
        return [dialog.tree.topLevelItem(i).text(0)
                for i in range(dialog.tree.topLevelItemCount())]

    def body():
        dialog = _dialog()
        _assert(groups(dialog) == ["ADJ", "Cameo", "Generic"],
                "manufacturers group by folder, root files under Generic")

        dialog._set_group_by(GROUP_BY_CATEGORY)
        _assert(groups(dialog) == ["Color Changer", "Moving Head"],
                "categories come from the documents")
        moving = dialog.tree.topLevelItem(1)
        color = dialog.tree.topLevelItem(0)
        _assert(moving.childCount() == 1 and color.childCount() == 3,
                "a fixture with two categories is listed under each")

    _with_tree({
        "ADJ/focus-spot-4z.json": RICH,
        "Cameo/wash-12.json": _doc("Wash 12"),
        "loose.json": _doc("loose"),
    }, body)


def test_search_spans_name_manufacturer_and_category(qapp):
    print("test_search_spans_name_manufacturer_and_category")

    def visible(dialog):
        return sorted(entry.key for entry in dialog._visible_entries())

    def body():
        dialog = _dialog()
        _assert(dialog.count_label.text() == "3 / 3", "the counter starts at everything")

        dialog.search_input.setText("cameo")
        _assert(visible(dialog) == ["Cameo/Wash 12"], "a manufacturer matches")
        _assert(dialog.count_label.text() == "1 / 3", "the counter follows the filter")

        dialog.search_input.setText("moving")
        _assert(visible(dialog) == ["ADJ/Focus Spot 4Z"], "a category matches")

        dialog.search_input.setText("spot 4z")
        _assert(visible(dialog) == ["ADJ/Focus Spot 4Z"],
                "every token has to match, in any field")

        dialog.search_input.setText("nothing here")
        _assert(visible(dialog) == [], "an unmatched search empties the tree")
        _assert(dialog._entry is not None,
                "but the detail panel keeps showing what was selected")

    _with_tree({
        "ADJ/focus-spot-4z.json": RICH,
        "Cameo/wash-12.json": _doc("Wash 12"),
        "loose.json": _doc("loose"),
    }, body)


def test_detail_panel_renders_the_document(qapp):
    print("test_detail_panel_renders_the_document")

    def body():
        dialog = _dialog()
        dialog.select_type("ADJ/Focus Spot 4Z")
        _assert(dialog.detail_name.text() == "Focus Spot 4Z", "the name is shown")
        _assert(dialog.detail_manufacturer.text() == "ADJ", "so is the manufacturer")
        _assert(dialog.stat_modes.text() == "2", "MODES counts every declared mode")
        _assert(dialog.stat_power.text() == "600 W", "POWER comes from `physical`")
        _assert(dialog._physical_body.count() == 7,
                "every physical row the document states is rendered")
        _assert(dialog._links_body.count() == 2, "one button per link")

        # The default mode is the first declared one.
        _assert(dialog.values()[3] == "Basic", "the default mode is preselected")
        _assert(dialog.channel_table.topLevelItemCount() == 3,
                "the channel table lists that mode's slots")
        dialog.set_mode("Extended")
        _assert(dialog.channel_table.topLevelItemCount() == 4,
                "picking another mode relays the table")
        _assert(dialog.channel_table.topLevelItem(0).text(0) == "01 · 1",
                "addresses are numbered from the DMX start")
        dialog.dmx_spin.setValue(80)
        _assert(dialog.channel_table.topLevelItem(0).text(0) == "01 · 80",
                "and follow the DMX start as it moves")
        _assert(dialog.channels_range.text() == "DMX 80 – 83",
                "the footprint caption follows too")

    _with_tree({"ADJ/focus-spot-4z.json": RICH}, body)


def test_footer_refuses_an_illegal_patch(qapp):
    print("test_footer_refuses_an_illegal_patch")
    from core.config import Config, FixtureInstance
    from stage.controller import StageController
    from ui.fixture.browser import AddFixtureDialog

    def body():
        config = Config()
        config.fixtures = [FixtureInstance(type="loose", dmx_start=10, channel=3)]
        dialog = AddFixtureDialog(StageController(config))
        dialog.select_type("loose")

        dialog.dmx_spin.setValue(20)
        dialog.channel_spin.setValue(0)
        _assert(dialog.dmx_status_label.text() == "free · no overlap",
                "a clear address reports free")
        _assert(dialog.ok_button.isEnabled(), "and OK is available")
        _assert(dialog.occupies_label.text() == "occupies 20 – 23",
                "the footprint is spelled out")

        dialog.dmx_spin.setValue(12)
        _assert("overlaps" in dialog.dmx_status_label.text(),
                "an address inside a patched fixture is refused")
        _assert(not dialog.ok_button.isEnabled(), "so OK is blocked")

        dialog.dmx_spin.setValue(510)
        _assert("past 512" in dialog.dmx_status_label.text(),
                "a fixture running past the universe is refused")
        _assert(not dialog.ok_button.isEnabled(), "so OK is blocked")

        dialog.dmx_spin.setValue(20)
        dialog.channel_spin.setValue(3)          # the patched fixture's channel
        _assert(dialog.channel_status_label.text() == "already used",
                "a MIDI channel driving another fixture is refused")
        _assert(not dialog.ok_button.isEnabled(), "so OK is blocked")

        dialog.channel_spin.setValue(5)
        _assert(dialog.ok_button.isEnabled(), "a free channel clears it")
        _assert(dialog.values()[2] == 5,
                "the channel is raw 0-15 — shown and stored the same")

    _with_tree({"loose.json": _doc("loose")}, body)


def main():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    test_subfolders_namespace_the_registry_key()
    test_a_sidecar_only_pairs_within_its_own_folder()
    test_ofl_document_metadata_is_parsed()
    test_browser_groups_by_manufacturer_and_by_category(app)
    test_search_spans_name_manufacturer_and_category(app)
    test_detail_panel_renders_the_document(app)
    test_footer_refuses_an_illegal_patch(app)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
