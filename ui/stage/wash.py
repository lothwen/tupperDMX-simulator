# ABOUTME: Compatibility shim — these renderers are now profiles.
"""Where the lamp renderers used to live. They are in `ui/stage/profiles/lamp.py`."""

from ui.stage.profiles.lamp import (  # noqa: F401
    ColorChangerFixture, DimmerFixture, LampFixture,
)

__all__ = ["ColorChangerFixture", "DimmerFixture", "LampFixture"]
