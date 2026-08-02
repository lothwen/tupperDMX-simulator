# ABOUTME: Compatibility shim — the fallback body is now a profile.
"""Where `GenericStageFixture` used to live. It is in `ui/stage/profiles/lamp.py`."""

from ui.stage.profiles.lamp import GenericStageFixture  # noqa: F401

__all__ = ["GenericStageFixture"]
