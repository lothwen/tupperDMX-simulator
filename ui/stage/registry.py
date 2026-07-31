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

from core.fixture_library import (
    FixtureDef, ROLE_BLUE, ROLE_BRIGHTNESS, ROLE_GREEN, ROLE_RED,
    ROLE_WHITE_COOL, ROLE_WHITE_WARM,
)
from tupperlogger import get_logger

logger = get_logger("ui.stage.registry")

_BY_CATEGORY: Dict[str, Type] = {}

_COLOUR_ROLES = frozenset({ROLE_RED, ROLE_GREEN, ROLE_BLUE,
                           ROLE_WHITE_WARM, ROLE_WHITE_COOL})


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
    from ui.stage.generic import GenericStageFixture
    from ui.stage.pixelbar import PixelBarFixture
    from ui.stage.wash import ColorChangerFixture, DimmerFixture

    for category in fdef.categories:
        if category in _BY_CATEGORY:
            return _BY_CATEGORY[category]

    roles = {channel.role for channel in fdef.channels}
    if fdef.matrix is not None:
        return PixelBarFixture
    if roles & _COLOUR_ROLES:
        return ColorChangerFixture
    if ROLE_BRIGHTNESS in roles:
        return DimmerFixture
    logger.debug(f"No renderer matches '{fdef.name}' {fdef.categories}; "
                 f"falling back to the generic body")
    return GenericStageFixture


def registered_categories() -> Dict[str, Type]:
    """The category -> renderer map (for tests and diagnostics)."""
    return dict(_BY_CATEGORY)
