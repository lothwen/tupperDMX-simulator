# ABOUTME: Capability features — imported here so registration happens on import.
"""What a fixture's channels do to the light its body makes."""

from ui.stage.features.base import (
    FEATURES, Feature, build_features, register_feature,
)

# Imported for their registration side effect.
from ui.stage.features import beamshape, level, wheels  # noqa: F401

__all__ = ["FEATURES", "Feature", "build_features", "register_feature"]
