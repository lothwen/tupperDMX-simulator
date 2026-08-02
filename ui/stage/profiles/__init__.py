# ABOUTME: Imports every profile module for its @register_renderer side effect.
"""Profiles: what a fixture looks like, chosen by its OFL category.

Importing this package is what populates the renderer registry. Adding a kind of
light is a new module here with `@register_renderer("<OFL category>")` on its
class — nothing in the registry or the stage controller changes.
"""

from ui.stage.profiles.array import (
    BlinderFixture, PixelBarFixture, StrobeFixture,
)
from ui.stage.profiles.atmospheric import (
    FanFixture, HazerFixture, StandFixture,
)
from ui.stage.profiles.lamp import (
    ColorChangerFixture, DimmerFixture, GenericStageFixture, LampFixture,
)
from ui.stage.profiles.motorised import (
    FlowerFixture, LaserFixture, MovingHeadFixture, ScannerFixture,
)

__all__ = [
    "BlinderFixture", "ColorChangerFixture", "DimmerFixture", "FanFixture",
    "FlowerFixture", "GenericStageFixture", "HazerFixture", "LampFixture",
    "LaserFixture", "MovingHeadFixture", "PixelBarFixture", "ScannerFixture",
    "StandFixture", "StrobeFixture",
]
