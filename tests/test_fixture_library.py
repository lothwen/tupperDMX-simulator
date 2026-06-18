"""
Characterization test: the data-driven fixture-type library.

Covers parsing fixture definitions (role/flag name -> int), rejecting malformed
defs, the user dir overriding the shipped dir by name, the built-in singletons
surviving in software_controller's FIXTURE_TYPES (identity preserved — note
routing depends on it), and config's channel-count / known-type helpers.

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

import fixture_library
from fixture_library import (
    parse_fixture_def, load_fixture_defs, clear_cache, role_index_map,
    ROLE_BRIGHTNESS, ROLE_RED, ROLE_GREEN, ROLE_BLUE,
)


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


PAR_RGB = {
    "name": "par_rgb",
    "render": "par",
    "channels": [
        {"field": "brightness", "role": "brightness", "flags": ["blend", "knob_level"]},
        {"field": "red", "role": "red", "flags": ["blend"]},
        {"field": "green", "role": "green", "flags": ["blend"]},
        {"field": "blue", "role": "blue", "flags": ["blend"]},
    ],
}


def test_parse_roles_and_flags():
    print("test_parse_roles_and_flags")
    fdef = parse_fixture_def(PAR_RGB)
    _assert(fdef is not None, "valid def parses")
    _assert(fdef.name == "par_rgb" and fdef.render == "par", "name + render parsed")
    _assert(fdef.channel_count == 4, "channel count = 4")
    _assert(fdef.channels[0].role == ROLE_BRIGHTNESS, "brightness role name -> int")
    _assert(fdef.channels[0].flags == (0x01 | 0x02), "blend|knob_level flags -> bitmask")
    _assert(fdef.channels[1].role == ROLE_RED, "red role name -> int")


def test_parse_rejects_malformed():
    print("test_parse_rejects_malformed")
    _assert(parse_fixture_def({}) is None, "empty dict rejected")
    _assert(parse_fixture_def({"name": "x"}) is None, "missing channels rejected")
    _assert(parse_fixture_def({"name": "x", "channels": []}) is None, "empty channels rejected")
    _assert(parse_fixture_def({"channels": [{"field": "a", "role": "red"}]}) is None,
            "missing name rejected")


def _patch_dirs(tmp):
    shipped = Path(tmp) / "shipped"
    user = Path(tmp) / "user"
    shipped.mkdir()
    user.mkdir()
    fixture_library.fixtures_dir_shipped = lambda: str(shipped)
    fixture_library.fixtures_dir_user = lambda: user
    clear_cache()
    return shipped, user


def test_load_and_user_override():
    print("test_load_and_user_override")
    orig_shipped = fixture_library.fixtures_dir_shipped
    orig_user = fixture_library.fixtures_dir_user
    try:
        with tempfile.TemporaryDirectory() as tmp:
            shipped, user = _patch_dirs(tmp)
            (shipped / "par_rgb.json").write_text(json.dumps(PAR_RGB))
            (shipped / "broken.json").write_text("{ not valid json")
            # The user dir overrides par_rgb with a 3-channel variant.
            user_def = dict(PAR_RGB)
            user_def["channels"] = PAR_RGB["channels"][:3]
            (user / "par_rgb.json").write_text(json.dumps(user_def))

            defs = load_fixture_defs(force_reload=True)
            _assert("par_rgb" in defs, "par_rgb loaded")
            _assert("broken" not in defs, "malformed json skipped")
            _assert(defs["par_rgb"].channel_count == 3, "user def overrides shipped by name")
    finally:
        fixture_library.fixtures_dir_shipped = orig_shipped
        fixture_library.fixtures_dir_user = orig_user
        clear_cache()


def test_role_index_map():
    print("test_role_index_map")
    idx = role_index_map(parse_fixture_def(PAR_RGB))
    _assert(idx[ROLE_BRIGHTNESS] == 0 and idx[ROLE_RED] == 1
            and idx[ROLE_GREEN] == 2 and idx[ROLE_BLUE] == 3,
            "role -> slice index mapping is correct")


def test_builtins_preserve_identity():
    print("test_builtins_preserve_identity")
    import software_controller.main as sc
    types = sc.build_fixture_types()
    _assert(types["ledbar"] is sc.FIXTURE_H807SA, "ledbar stays the H807SA singleton")
    _assert(types["blinder"] is sc.FIXTURE_BLINDER, "blinder stays the blinder singleton")


def test_config_channel_helpers():
    print("test_config_channel_helpers")
    import config
    _assert(config.fixture_channel_count("ledbar") == 7, "ledbar = 7 channels")
    _assert(config.fixture_channel_count("blinder") == 8, "blinder = 8 channels")
    _assert(config.fixture_channel_count("bogus") == 0, "unknown type = 0 channels")
    _assert({"ledbar", "blinder"} <= config.known_fixture_types(),
            "known types include the built-ins")


def main():
    test_parse_roles_and_flags()
    test_parse_rejects_malformed()
    test_load_and_user_override()
    test_role_index_map()
    test_builtins_preserve_identity()
    test_config_channel_helpers()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
