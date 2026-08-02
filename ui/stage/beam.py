# ABOUTME: Compatibility shim — the beam cone now lives in ui/stage/emission/.
"""Where the beam cone used to live.

Kept because the scene preview sizes its viewport from `BEAM_LENGTH_PX`, and
because a module path is an API. The cone itself is an emission model now.
"""

from ui.stage.emission.cone import (  # noqa: F401
    BEAM_MAX_ALPHA, BEAM_MIN_INTENSITY, BeamCone,
)
from ui.stage.frame import BEAM_LENGTH_PX  # noqa: F401

__all__ = ["BEAM_LENGTH_PX", "BEAM_MAX_ALPHA", "BEAM_MIN_INTENSITY", "BeamCone"]
