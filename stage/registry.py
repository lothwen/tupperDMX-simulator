# ABOUTME: Maps a fixture's OFL categories onto the stage renderer that draws it.
"""Renderer selection.

Which class draws a fixture is decided by what the fixture *says it is* — its
OFL `categories` — not by its name. Adding support for a new kind of light is a
new module with `@register_renderer("Moving Head")` on it; nothing here or in
the stage controller changes.

A fixture lists its categories most-important-first, so that order wins. Only
when none of them has a renderer do we fall back to reading its channels.
"""

from typing import Dict, Type

from core.fixture_library import FixtureDef, all_capability_types
from tupperlogger import get_logger

logger = get_logger("stage.registry")

_BY_CATEGORY: Dict[str, Type] = {}

# Capabilities that make a fixture a colour changer, whichever colours it mixes.
_COLOUR_CAPABILITIES = frozenset({"ColorIntensity", "ColorPreset",
                                  "ColorTemperature"})


def register_renderer(*categories: str):
    """Class decorator claiming one or more OFL categories."""
    def decorate(cls: Type) -> Type:
        for category in categories:
            _BY_CATEGORY[category] = cls
        return cls
    return decorate


def renderer_for(fdef: FixtureDef) -> Type:
    """The renderer class for a fixture definition.

    Falls back to inferring from the fixture's channels, so a definition
    categorised only as "Other" — or as something nobody has written a renderer
    for yet — still draws as the kind of light its channels describe.
    """
    from stage.profiles.array import PixelBarFixture
    from stage.profiles.lamp import (
        ColorChangerFixture, DimmerFixture, GenericStageFixture,
    )

    for category in fdef.categories:
        if category in _BY_CATEGORY:
            return _BY_CATEGORY[category]

    # Nothing claimed the category. Read the channels instead, most specific
    # first — a fixture that pans is a mover whatever it calls itself, and a
    # fixture that makes fog is a hazer. `all_capability_types` and not the
    # dominant summary: a pan channel is a pan channel even if it spends most of
    # its range on a program.
    types = all_capability_types(fdef)
    if fdef.matrix is not None:
        return PixelBarFixture
    for capabilities, renderer in _CHANNEL_SHAPE_FALLBACKS():
        if types & capabilities:
            return renderer
    if types & _COLOUR_CAPABILITIES:
        return ColorChangerFixture
    if "Intensity" in types:
        return DimmerFixture
    logger.debug(f"No renderer matches '{fdef.name}' {fdef.categories}; "
                 f"falling back to the generic body")
    return GenericStageFixture


def _CHANNEL_SHAPE_FALLBACKS():
    """Capability sets that identify a kind of fixture, most specific first.

    A function rather than a constant so the profile modules are imported
    lazily, which keeps this module free of the import cycle they close.
    """
    from stage.profiles.atmospheric import HazerFixture
    from stage.profiles.motorised import MovingHeadFixture

    return (
        # Something that fills the air is a hazer even if it calls itself
        # "Other" — and it matters, because its output is what makes every
        # other fixture's beam visible.
        (frozenset({"Fog", "FogOutput", "FogType"}), HazerFixture),
        # Something that aims is a mover. Which kind of beam it draws is then
        # decided by the rest of its channels, inside the profile.
        (frozenset({"Pan", "Tilt", "PanContinuous", "TiltContinuous"}),
         MovingHeadFixture),
    )


def registered_categories() -> Dict[str, Type]:
    """The category -> renderer map (for tests and diagnostics)."""
    return dict(_BY_CATEGORY)
