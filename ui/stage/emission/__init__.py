# ABOUTME: Emission models — the kinds of light a fixture body can make.
"""How a fixture's light is drawn, as distinct from the fixture itself."""

from ui.stage.emission.base import Emission, NoEmission, emission_reach
from ui.stage.emission.cone import (
    BeamCone, BeamFan, HardBeam, LampGlow, WashLamp, emission_for_kind,
)
from ui.stage.emission.volumetric import FogPlume, LaserFan
from ui.stage.emission.surface import BulbArray, FlatPanel, MatrixCells

__all__ = ["Emission", "NoEmission", "emission_reach", "emission_for_kind",
           "BeamCone", "BeamFan", "HardBeam", "LampGlow",
           "WashLamp", "FogPlume", "LaserFan",
           "BulbArray", "FlatPanel", "MatrixCells"]
