# ABOUTME: Compatibility shim — this renderer is now a profile.
"""Where `StrobeFixture` used to live. It is in `ui/stage/profiles/array.py` now."""

from ui.stage.profiles.array import StrobeFixture  # noqa: F401

__all__ = ["StrobeFixture"]
