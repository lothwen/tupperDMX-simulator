"""
Characterization test: per-fixture OFL modes.

An OFL fixture can declare several modes (4ch / 7ch / 16ch), each a different
DMX layout of the same product. A patched fixture stores which one it uses, and
that choice has to reach everything downstream: the channel count it occupies,
the notes its clips can bind to, the controller's compiled type, and the stage.

The rules being pinned:

  * an empty mode means "the type's default", so existing patches — which have
    no mode at all — keep working;
  * an unknown mode falls back to the default and does NOT drop the fixture,
    because a dropped fixture disappears from the show silently;
  * the stored value stays "" rather than the resolved name, so renaming a mode
    in a definition can never strand a saved patch;
  * a fixture that would run past the controller's universe is refused at patch
    time rather than raising inside the 20 Hz DMX thread.

Run with:
    uv run python tests/test_fixture_modes.py
"""

import sys
import os
import json
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import fixture_library
from core.fixture_library import (
    clear_cache, fixture_modes, load_fixture_defs, resolve_fixture,
)
from core.config import Config, FixtureInstance, fixture_channel_count

SCHEMA = "https://raw.githubusercontent.com/OpenLightingProject/open-fixture-library/master/schemas/fixture.json"
META = {"authors": ["test"], "createDate": "2026-01-01", "lastModifyDate": "2026-01-01"}

MULTIMODE = {
    "$schema": SCHEMA,
    "name": "multi",
    "categories": ["Color Changer", "Dimmer"],
    "meta": META,
    "availableChannels": {
        "brightness": {"capability": {"type": "Intensity"}},
        "red": {"capability": {"type": "ColorIntensity", "color": "Red"}},
        "green": {"capability": {"type": "ColorIntensity", "color": "Green"}},
        "blue": {"capability": {"type": "ColorIntensity", "color": "Blue"}},
    },
    "modes": [
        {"name": "4ch", "channels": ["brightness", "red", "green", "blue"]},
        {"name": "3ch", "channels": ["red", "green", "blue"]},
    ],
}

# A mode wide enough to overflow the controller's 96-channel universe.
HUNGRY = {
    "$schema": SCHEMA,
    "name": "hungry",
    "categories": ["Other"],
    "meta": META,
    "availableChannels": {f"c{i}": {"capability": {"type": "Generic"}}
                          for i in range(60)},
    "modes": [{"name": "60ch", "channels": [f"c{i}" for i in range(60)]}],
}


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _with_defs(docs, body):
    """Run `body()` with `docs` as the only shipped fixture definitions."""
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
            for doc in docs:
                (shipped / f"{doc['name']}.json").write_text(json.dumps(doc))
            load_fixture_defs(force_reload=True)
            body()
    finally:
        fixture_library.fixtures_dir_shipped = orig_shipped
        fixture_library.fixtures_dir_user = orig_user
        clear_cache()


def test_channel_count_follows_the_mode():
    print("test_channel_count_follows_the_mode")

    def body():
        _assert(fixture_modes("multi") == ["4ch", "3ch"], "both modes are offered")
        _assert(fixture_channel_count("multi") == 4, "no mode -> the first declared")
        _assert(fixture_channel_count("multi", "4ch") == 4, "4ch occupies 4 channels")
        _assert(fixture_channel_count("multi", "3ch") == 3, "3ch occupies 3")
        _assert(fixture_channel_count("multi", "nope") == 4,
                "an unknown mode falls back to the default")

    _with_defs([MULTIMODE], body)


def test_mode_changes_which_channels_exist():
    print("test_mode_changes_which_channels_exist")

    def body():
        wide = resolve_fixture("multi", "4ch")
        narrow = resolve_fixture("multi", "3ch")
        _assert([c.field for c in wide.channels] == ["brightness", "red", "green", "blue"],
                "4ch exposes the brightness channel")
        _assert([c.field for c in narrow.channels] == ["red", "green", "blue"],
                "3ch drops it, shifting every later channel's address")
        _assert(wide.mode == "4ch" and narrow.mode == "3ch",
                "each resolved definition records its own mode")

    _with_defs([MULTIMODE], body)


def test_patch_parsing_validates_the_mode():
    print("test_patch_parsing_validates_the_mode")

    def body():
        parsed = Config._parse_fixtures([
            {"type": "multi", "dmx_start": 1, "mode": "3ch"},
            {"type": "multi", "dmx_start": 10, "mode": "bogus"},
            {"type": "multi", "dmx_start": 20},
            {"type": "gone", "dmx_start": 30},
        ])
        _assert(len(parsed) == 3, "an unknown TYPE is dropped, an unknown MODE is not")
        _assert(parsed[0].mode == "3ch", "a valid mode is kept")
        _assert(parsed[1].mode == "",
                "an unknown mode falls back to the default without losing the fixture")
        _assert(parsed[2].mode == "", "a patch with no mode at all still parses")

    _with_defs([MULTIMODE], body)


def test_mode_survives_a_manifest_roundtrip():
    print("test_mode_survives_a_manifest_roundtrip")
    from dataclasses import asdict

    def body():
        original = FixtureInstance(type="multi", dmx_start=1, channel=0, mode="3ch")
        restored = Config._parse_fixtures([asdict(original)])[0]
        _assert(restored.mode == "3ch", "mode is written and read back")
        _assert(restored.dmx_start == 1 and restored.channel == 0,
                "the rest of the instance is unchanged")

    _with_defs([MULTIMODE], body)


def test_used_dmx_channels_follow_the_mode():
    print("test_used_dmx_channels_follow_the_mode")

    def body():
        config = Config()
        config.fixtures = [
            FixtureInstance(type="multi", dmx_start=1, channel=0, mode="4ch"),
            FixtureInstance(type="multi", dmx_start=20, channel=1, mode="3ch"),
        ]
        used = config.get_used_dmx_channels()
        spans = sorted(len(v) for v in used.values())
        _assert(spans == [3, 4], "each fixture reserves its own mode's width")

    _with_defs([MULTIMODE], body)


def test_controller_compiles_per_mode():
    print("test_controller_compiles_per_mode")
    import software_controller.main as sc

    def body():
        _assert(sc.fixture_type_for("multi", "4ch").channel_count == 4,
                "the controller compiles the 4ch layout")
        _assert(sc.fixture_type_for("multi", "3ch").channel_count == 3,
                "and the 3ch layout separately")
        _assert(sc.fixture_type_for("nope") is None, "an unknown type compiles to None")

    _with_defs([MULTIMODE], body)


def test_controller_type_cache_follows_library_reloads():
    print("test_controller_type_cache_follows_library_reloads")
    import software_controller.main as sc

    # The compiled types used to be built once at import, so a show opened later
    # never contributed its show-local definitions to the controller.
    def body_wide():
        _assert(sc.fixture_type_for("multi").channel_count == 4,
                "compiles against the definitions loaded now")

    _with_defs([MULTIMODE], body_wide)

    narrowed = dict(MULTIMODE)
    narrowed["modes"] = [{"name": "3ch", "channels": ["red", "green", "blue"]}]

    def body_narrow():
        _assert(sc.fixture_type_for("multi").channel_count == 3,
                "a reloaded library invalidates the controller's compiled types")

    _with_defs([narrowed], body_narrow)


def test_patch_refuses_a_fixture_past_the_universe():
    print("test_patch_refuses_a_fixture_past_the_universe")
    import software_controller.main as sc

    def body():
        controller = sc.DMXController()
        # 60 channels at address 1 fits; at address 50 it would run off the end
        # of the 96-channel universe and index out of range in the DMX thread.
        controller.init_fixtures([
            FixtureInstance(type="hungry", dmx_start=1, channel=0),
            FixtureInstance(type="hungry", dmx_start=50, channel=1),
        ])
        _assert(len(controller.fixtures) == 1,
                "the fixture that would overflow the universe is skipped")
        _assert(controller.fixtures[0].dmx_base == 1, "the one that fits is patched")

    _with_defs([HUNGRY], body)


def test_add_fixture_dialog_offers_every_mode(qapp):
    print("test_add_fixture_dialog_offers_every_mode")
    from core.config import Config as RealConfig
    from stage.controller import StageController
    from ui.fixture_designer import AddFixtureDialog

    def body():
        stage = StageController(RealConfig())
        dialog = AddFixtureDialog(stage)

        dialog.select_type("multi")
        _assert([name for _b, name, _c, _l in dialog._mode_buttons] == ["4ch", "3ch"],
                "the mode list matches the definition")
        _assert(dialog.values()[3] == "4ch",
                "the default mode is returned without touching anything")
        _assert(dialog.stat_channels.text() == "4", "the header counts its channels")

        dialog.set_mode("3ch")
        _assert(dialog.values()[3] == "3ch", "picking a mode changes the result")
        _assert(dialog.stat_channels.text() == "3", "and the channel count follows")

    _with_defs([MULTIMODE], body)


def test_add_fixture_stores_empty_mode_for_single_mode_types(qapp):
    print("test_add_fixture_stores_empty_mode_for_single_mode_types")
    from core.config import Config as RealConfig
    from stage.controller import StageController
    from ui.fixture_designer import AddFixtureDialog

    single = dict(MULTIMODE)
    single["name"] = "solo"
    single["modes"] = [{"name": "4ch", "channels": ["brightness", "red", "green", "blue"]}]

    def body():
        stage = StageController(RealConfig())
        dialog = AddFixtureDialog(stage)
        dialog.select_type("solo")
        _assert(len(dialog._mode_buttons) == 1,
                "a single-mode type has nothing to pick")
        _assert(dialog.values()[3] == "",
                "and stores no mode, so renaming it can't strand the patch")

    _with_defs([single], body)


def test_changing_mode_rebuilds_the_render_group(qapp):
    print("test_changing_mode_rebuilds_the_render_group")
    from core.config import Config as RealConfig
    from stage.controller import StageController

    def body():
        config = RealConfig()
        config.fixtures = []
        stage = StageController(config)
        inst = stage.add_fixture("multi", 1, 100, 100, channel=0, mode="4ch")
        _assert(len(stage.generic_fixtures) == 1, "the fixture renders")
        _assert(stage.generic_fixtures[0].dmx_channel_count == 4,
                "its render object reads a 4-channel slice")

        stage.change_fixture_mode(inst, "3ch")
        _assert(inst.mode == "3ch", "the instance records the new mode")
        _assert(len(stage.generic_fixtures) == 1, "still exactly one render object")
        _assert(stage.generic_fixtures[0].dmx_channel_count == 3,
                "rebuilt against the new mode's channel count")

    _with_defs([MULTIMODE], body)


def main():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    test_channel_count_follows_the_mode()
    test_mode_changes_which_channels_exist()
    test_patch_parsing_validates_the_mode()
    test_mode_survives_a_manifest_roundtrip()
    test_used_dmx_channels_follow_the_mode()
    test_controller_compiles_per_mode()
    test_controller_type_cache_follows_library_reloads()
    test_patch_refuses_a_fixture_past_the_universe()
    test_add_fixture_dialog_offers_every_mode(app)
    test_add_fixture_stores_empty_mode_for_single_mode_types(app)
    test_changing_mode_rebuilds_the_render_group(app)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
