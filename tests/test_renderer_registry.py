"""
Characterization test: how a fixture picks the renderer that draws it.

The point of the stage engine is that adding a fixture type takes no Python: a
definition's OFL `categories` decide which renderer draws it. This covers that
selection, including what happens for a fixture whose categories nobody has
written a renderer for — it must still appear on stage, drawn from the shape of
its channels rather than dropped.

Run with:
    uv run python tests/test_renderer_registry.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import ofl
from core.fixture_library import build_fixture_def, resolve_fixture
from ui.stage import (
    BlinderFixture, ColorChangerFixture, DimmerFixture, GenericStageFixture,
    PixelBarFixture, StrobeFixture, registered_categories, renderer_for,
)

SCHEMA = "https://raw.githubusercontent.com/OpenLightingProject/open-fixture-library/master/schemas/fixture.json"
META = {"authors": ["test"], "createDate": "2026-01-01", "lastModifyDate": "2026-01-01"}
WILD_WASH = "Wild Wash Pro 648 RGBW"


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _fdef(categories, channels=None, matrix=None):
    doc = {
        "$schema": SCHEMA, "name": "probe", "categories": list(categories),
        "meta": META,
        "availableChannels": channels or {"d": {"capability": {"type": "Intensity"}}},
        "modes": [{"name": "test", "channels": list(channels or {"d": None})}],
    }
    if matrix:
        doc["matrix"] = matrix
        doc["templateChannels"] = {"$pixelKey d": {"capability": {"type": "Intensity"}}}
    return build_fixture_def("probe", ofl.parse_ofl(doc))


RGB_CHANNELS = {
    "r": {"capability": {"type": "ColorIntensity", "color": "Red"}},
    "g": {"capability": {"type": "ColorIntensity", "color": "Green"}},
    "b": {"capability": {"type": "ColorIntensity", "color": "Blue"}},
}


def test_category_selects_the_renderer():
    print("test_category_selects_the_renderer")
    cases = [
        (["Pixel Bar"], PixelBarFixture),
        (["Matrix"], PixelBarFixture),
        (["Blinder"], BlinderFixture),
        (["Strobe"], StrobeFixture),
        (["Color Changer"], ColorChangerFixture),
        (["Dimmer"], DimmerFixture),
    ]
    for categories, expected in cases:
        _assert(renderer_for(_fdef(categories)) is expected,
                f"{categories[0]} -> {expected.__name__}")


def test_the_fixtures_own_category_order_wins():
    print("test_the_fixtures_own_category_order_wins")
    # OFL lists the most important category first, so that is the one that
    # decides — not any preference of ours.
    _assert(renderer_for(_fdef(["Pixel Bar", "Color Changer", "Strobe"])) is PixelBarFixture,
            "a bar that also changes colour is drawn as a bar")
    _assert(renderer_for(_fdef(["Color Changer", "Pixel Bar"])) is ColorChangerFixture,
            "reversing the order reverses the choice")
    _assert(renderer_for(_fdef(["Blinder", "Dimmer"])) is BlinderFixture,
            "a blinder that also dims is drawn as a blinder")


def test_unknown_categories_fall_back_to_channel_shape():
    print("test_unknown_categories_fall_back_to_channel_shape")
    # Nothing renders a "Fan" or a "Hazer" yet; such a fixture still has to
    # appear, drawn from what its channels say it does.
    _assert(renderer_for(_fdef(["Fan"], RGB_CHANNELS)) is ColorChangerFixture,
            "colour channels make it a colour changer")
    _assert(renderer_for(_fdef(["Hazer"])) is DimmerFixture,
            "a lone intensity channel makes it a dimmer")
    _assert(renderer_for(_fdef(["Other"], matrix={"pixelCount": [4, 2, 1]}))
            is PixelBarFixture,
            "a pixel matrix makes it a bar whatever its category says")
    _assert(renderer_for(_fdef(["Other"], {"x": {"capability": {"type": "Maintenance"}}}))
            is GenericStageFixture,
            "a fixture with no light-shaped channel at all still gets a body")


def test_shipped_fixtures_pick_the_expected_renderers():
    print("test_shipped_fixtures_pick_the_expected_renderers")
    expected = {
        "ledbar": PixelBarFixture,
        "blinder": BlinderFixture,
        "par_rgb": ColorChangerFixture,
        WILD_WASH: ColorChangerFixture,
    }
    for type_name, renderer in expected.items():
        _assert(renderer_for(resolve_fixture(type_name)) is renderer,
                f"{type_name} -> {renderer.__name__}")


def test_registry_is_populated_by_importing_the_package():
    print("test_registry_is_populated_by_importing_the_package")
    categories = registered_categories()
    _assert({"Pixel Bar", "Matrix", "Blinder", "Strobe", "Color Changer", "Dimmer"}
            <= set(categories),
            "every shipped renderer registered its categories on import")
    _assert(GenericStageFixture not in categories.values(),
            "the fallback claims no category — it is only reached by fallthrough")


def main():
    test_category_selects_the_renderer()
    test_the_fixtures_own_category_order_wins()
    test_unknown_categories_fall_back_to_channel_shape()
    test_shipped_fixtures_pick_the_expected_renderers()
    test_registry_is_populated_by_importing_the_package()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
