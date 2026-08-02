# ABOUTME: Compatibility shim — this renderer is now a profile.
"""Where `PixelBarFixture` used to live. It is in `ui/stage/profiles/array.py` now."""

from ui.stage.profiles.array import PixelBarFixture  # noqa: F401

__all__ = ["PixelBarFixture"]
