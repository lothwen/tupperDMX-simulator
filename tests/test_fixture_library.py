"""
Characterization test: the fixture-definition loader.

Covers the parts that involve real files on disk — the three layers (shipped,
user, show-local) and how a definition's two files (the OFL document and its
optional .tdmx.json sidecar) are paired within a layer — plus the built-in types
reaching the software controller and config's channel-count helpers.

The format itself is covered by tests/test_ofl_parser.py, and the OFL -> role
translation by tests/test_ofl_bridge.py.

Paths are redirected to a temp dir and restored afterwards (the loader memoizes,
so the cache is cleared on the way in and out).

Run with:
    uv run python tests/test_fixture_library.py
"""

import sys
import os
import json
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import fixture_library
from core.fixture_library import (
    load_fixture_defs, clear_cache, role_index_map, resolve_fixture, fixture_modes,
    ROLE_BRIGHTNESS, ROLE_RED, ROLE_GREEN, ROLE_BLUE,
)

SCHEMA = "https://raw.githubusercontent.com/OpenLightingProject/open-fixture-library/master/schemas/fixture.json"
META = {"authors": ["test"], "createDate": "2026-01-01", "lastModifyDate": "2026-01-01"}

PAR_RGB = {
    "$schema": SCHEMA,
    "name": "par_rgb",
    "categories": ["Color Changer", "Dimmer"],
    "meta": META,
    "availableChannels": {
        "brightness": {"capability": {"type": "Intensity"}},
        "red": {"capability": {"type": "ColorIntensity", "color": "Red"}},
        "green": {"capability": {"type": "ColorIntensity", "color": "Green"}},
        "blue": {"capability": {"type": "ColorIntensity", "color": "Blue"}},
    },
    "modes": [{"name": "4ch", "channels": ["brightness", "red", "green", "blue"]},
              {"name": "3ch", "channels": ["red", "green", "blue"]}],
}

PAR_RGB_SIDECAR = {
    "tdmx": 1,
    "modes": {"*": {"banks": {"SHIPPED": [{"brightness": 255}]}}},
}


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _patch_dirs(tmp):
    shipped = Path(tmp) / "shipped"
    user = Path(tmp) / "user"
    shipped.mkdir()
    user.mkdir()
    fixture_library.fixtures_dir_shipped = lambda: str(shipped)
    fixture_library.fixtures_dir_user = lambda: user
    clear_cache()
    return shipped, user


def _write(directory, name, data):
    (directory / name).write_text(json.dumps(data))


def _with_temp_layers(body):
    """Run `body(shipped, user)` against redirected fixture directories."""
    orig_shipped = fixture_library.fixtures_dir_shipped
    orig_user = fixture_library.fixtures_dir_user
    try:
        with tempfile.TemporaryDirectory() as tmp:
            body(*_patch_dirs(tmp))
    finally:
        fixture_library.fixtures_dir_shipped = orig_shipped
        fixture_library.fixtures_dir_user = orig_user
        clear_cache()


def test_load_and_user_override():
    print("test_load_and_user_override")

    def body(shipped, user):
        _write(shipped, "par_rgb.json", PAR_RGB)
        (shipped / "broken.json").write_text("{ not valid json")
        # A legacy-format def no longer resolves — the OFL-only cut.
        _write(shipped, "old.json", {"name": "old", "render": "par",
                                     "channels": [{"field": "a", "role": "red"}]})
        # The user layer overrides par_rgb with a 3-channel variant.
        user_def = dict(PAR_RGB)
        user_def["modes"] = [{"name": "3ch", "channels": ["red", "green", "blue"]}]
        _write(user, "par_rgb.json", user_def)

        defs = load_fixture_defs(force_reload=True)
        _assert("par_rgb" in defs, "par_rgb loaded")
        _assert("broken" not in defs, "malformed json skipped")
        _assert("old" not in defs, "a pre-OFL definition is skipped, not parsed")
        _assert(defs["par_rgb"].channel_count == 3, "user def overrides shipped by name")

    _with_temp_layers(body)


def test_sidecar_pairs_within_its_own_layer():
    print("test_sidecar_pairs_within_its_own_layer")

    def body(shipped, user):
        _write(shipped, "par_rgb.json", PAR_RGB)
        _write(shipped, "par_rgb.tdmx.json", PAR_RGB_SIDECAR)
        _write(user, "par_rgb.json", PAR_RGB)   # overrides, with no sidecar

        defs = load_fixture_defs(force_reload=True)
        # A user override that drops the sidecar deliberately loses the banks;
        # inheriting the shipped one could pair banks with different channels.
        _assert(defs["par_rgb"].banks == {},
                "an override without a sidecar does not inherit the shipped one")

    _with_temp_layers(body)

    def body_paired(shipped, _user):
        _write(shipped, "par_rgb.json", PAR_RGB)
        _write(shipped, "par_rgb.tdmx.json", PAR_RGB_SIDECAR)
        defs = load_fixture_defs(force_reload=True)
        _assert(list(defs["par_rgb"].banks) == ["SHIPPED"],
                "a sidecar beside its definition supplies the banks")

    _with_temp_layers(body_paired)


def test_orphan_sidecar_is_ignored():
    print("test_orphan_sidecar_is_ignored")

    def body(shipped, _user):
        _write(shipped, "par_rgb.json", PAR_RGB)
        _write(shipped, "ghost.tdmx.json", {"tdmx": 1, "modes": {}})
        defs = load_fixture_defs(force_reload=True)
        _assert(set(defs) == {"par_rgb"},
                "a sidecar with no definition beside it adds nothing")

    _with_temp_layers(body)


def test_resolve_fixture_and_modes():
    print("test_resolve_fixture_and_modes")

    def body(shipped, _user):
        _write(shipped, "par_rgb.json", PAR_RGB)
        load_fixture_defs(force_reload=True)

        _assert(fixture_modes("par_rgb") == ["4ch", "3ch"],
                "declared modes are listed in order")
        _assert(fixture_modes("bogus") == [], "an unknown type has no modes")
        _assert(resolve_fixture("par_rgb").channel_count == 4,
                "no mode given -> the first declared mode")
        _assert(resolve_fixture("par_rgb", "3ch").channel_count == 3,
                "a named mode resolves to its own channel count")
        _assert(resolve_fixture("par_rgb", "nope").channel_count == 4,
                "an unknown mode falls back rather than failing")
        _assert(resolve_fixture("bogus") is None, "an unknown type resolves to None")

    _with_temp_layers(body)


def test_role_index_map():
    print("test_role_index_map")

    def body(shipped, _user):
        _write(shipped, "par_rgb.json", PAR_RGB)
        load_fixture_defs(force_reload=True)
        idx = role_index_map(resolve_fixture("par_rgb"))
        _assert(idx[ROLE_BRIGHTNESS] == 0 and idx[ROLE_RED] == 1
                and idx[ROLE_GREEN] == 2 and idx[ROLE_BLUE] == 3,
                "role -> slice index mapping is correct")

    _with_temp_layers(body)


def test_builtin_types_data_driven():
    print("test_builtin_types_data_driven")
    import software_controller.main as sc
    types = sc.build_fixture_types()
    _assert("ledbar" in types and "blinder" in types,
            "ledbar + blinder built from the fixture library")
    _assert(types["ledbar"].channel_count == 7, "ledbar has 7 channels")
    _assert(types["blinder"].channel_count == 8, "blinder has 8 channels")
    _assert("ANIMATIONS" in types["ledbar"].banks and bool(types["ledbar"].chases),
            "ledbar scene banks + chases loaded from its sidecar")
    _assert("BLINDER_SCENES" in types["blinder"].banks,
            "blinder scene bank loaded from its sidecar")


def test_config_channel_helpers():
    print("test_config_channel_helpers")
    from core import config
    _assert(config.fixture_channel_count("ledbar") == 7, "ledbar = 7 channels")
    _assert(config.fixture_channel_count("blinder") == 8, "blinder = 8 channels")
    _assert(config.fixture_channel_count("bogus") == 0, "unknown type = 0 channels")
    _assert({"ledbar", "blinder"} <= config.known_fixture_types(),
            "known types include the built-ins")


def main():
    test_load_and_user_override()
    test_sidecar_pairs_within_its_own_layer()
    test_orphan_sidecar_is_ignored()
    test_resolve_fixture_and_modes()
    test_role_index_map()
    test_builtin_types_data_driven()
    test_config_channel_helpers()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
