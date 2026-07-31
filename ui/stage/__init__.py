# ABOUTME: The stage render engine — category-driven fixture renderers.
"""Stage rendering.

Importing this package registers every renderer, so `renderer_for(fdef)` can
resolve any fixture. A new kind of light is a new module here with
`@register_renderer("<OFL category>")` on its class, listed below; nothing in
the stage controller needs to change.
"""

from ui.stage.base import (
    BodyFixture, DesignableFixture, FixtureLabel, GlowingEllipse, StrobeGate,
    body_count, body_size, body_spacing,
)
from ui.stage.registry import register_renderer, registered_categories, renderer_for

# Imported for their registration side effect; renderer_for() resolves by
# category, never by name.
from ui.stage.blinder import BlinderFixture
from ui.stage.generic import GenericStageFixture
from ui.stage.pixelbar import PixelBarFixture
from ui.stage.strobe import StrobeFixture
from ui.stage.wash import ColorChangerFixture, DimmerFixture

__all__ = [
    "BodyFixture", "DesignableFixture", "FixtureLabel", "GlowingEllipse",
    "StrobeGate", "body_count", "body_size", "body_spacing",
    "register_renderer", "registered_categories", "renderer_for",
    "BlinderFixture", "ColorChangerFixture", "DimmerFixture",
    "GenericStageFixture", "PixelBarFixture", "StrobeFixture",
]
