# ABOUTME: The stage render engine — category picks the body, capabilities shape
# ABOUTME: the light it makes.
"""Stage rendering.

Two stages, in this order:

1. a fixture's OFL **category** picks its *profile* — the shape of its body and
   the kind of light it emits (a beam, a wash, a grid of cells, a plume, none);
2. the **capabilities** its channels declare pick its *features* — the layers
   that dim, colour, chop, shape and aim that light.

Importing this package registers every profile and every feature, so
`renderer_for(fdef)` can resolve any fixture. Adding a kind of light is a new
module under `profiles/`; adding support for a capability is a new module under
`features/`. Nothing in the stage controller changes for either.
"""

from ui.stage.base import (
    BodyFixture, DesignableFixture, FixtureLabel, GlowingEllipse,
    body_count, body_size, body_spacing, declared_cell_count,
)
from ui.stage.atmosphere import DEFAULT_ATMOSPHERE, Atmosphere
from ui.stage.clock import CLOCK, AnimationClock
from ui.stage.frame import BEAM_LENGTH_PX, BeamParams, Frame
from ui.stage.geometry import Aim, Slew, Spinner
from ui.stage.optics import BeamKind, beam_kind, resting_half_angle
from ui.stage.registry import (
    register_renderer, registered_categories, renderer_for,
)

# Imported for their registration side effect; renderer_for() resolves by
# category, never by name.
from ui.stage.features import FEATURES, Feature, build_features
from ui.stage.profiles import (
    BlinderFixture, ColorChangerFixture, DimmerFixture, FanFixture,
    FlowerFixture, GenericStageFixture, HazerFixture, LampFixture,
    LaserFixture, MovingHeadFixture, PixelBarFixture, ScannerFixture,
    StandFixture, StrobeFixture,
)

__all__ = [
    "BodyFixture", "DesignableFixture", "FixtureLabel", "GlowingEllipse",
    "body_count", "body_size", "body_spacing", "declared_cell_count",
    "Atmosphere", "DEFAULT_ATMOSPHERE", "AnimationClock", "CLOCK",
    "BEAM_LENGTH_PX", "BeamParams", "Frame", "Aim", "Slew", "Spinner",
    "BeamKind", "beam_kind", "resting_half_angle",
    "register_renderer", "registered_categories", "renderer_for",
    "FEATURES", "Feature", "build_features",
    "BlinderFixture", "ColorChangerFixture", "DimmerFixture", "FanFixture",
    "FlowerFixture", "GenericStageFixture", "HazerFixture", "LampFixture",
    "LaserFixture", "MovingHeadFixture", "PixelBarFixture", "ScannerFixture",
    "StandFixture", "StrobeFixture",
]
