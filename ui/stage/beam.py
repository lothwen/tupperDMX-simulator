# ABOUTME: The light cone drawn in front of a fixture that declares a lens.
"""Beam cone.

A fixture that declares a lens angle (or carries a zoom channel) throws a
visible cone. Fixtures with no lens in their definition — the LED bar — get no
cone at all, which is what they looked like before.
"""

from typing import Optional

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QBrush, QColor, QLinearGradient, QPen, QPolygonF
from PyQt6.QtWidgets import QGraphicsPolygonItem

import math

from core.fixture_state import FixtureState

BEAM_LENGTH_PX = 320       # how far the cone reaches into the stage
BEAM_MAX_ALPHA = 90        # opacity at the lens, fading to nothing at the end
BEAM_MIN_INTENSITY = 0.02  # below this the cone is hidden entirely


class BeamCone(QGraphicsPolygonItem):
    """A soft cone of light spreading downstage from its parent fixture."""

    def __init__(self, parent, origin_x: float, origin_y: float):
        super().__init__(parent)
        self._origin = QPointF(origin_x, origin_y)
        self.setPen(QPen(Qt.PenStyle.NoPen))
        self.setZValue(-1)     # behind the fixture body
        self.setVisible(False)

    def apply_state(self, state: FixtureState) -> None:
        angle = _half_angle(state)
        if angle is None or state.intensity <= BEAM_MIN_INTENSITY:
            self.setVisible(False)
            return

        spread = math.tan(math.radians(angle)) * BEAM_LENGTH_PX
        tip_y = self._origin.y() + BEAM_LENGTH_PX
        self.setPolygon(QPolygonF([
            self._origin,
            QPointF(self._origin.x() - spread, tip_y),
            QPointF(self._origin.x() + spread, tip_y),
        ]))

        gradient = QLinearGradient(self._origin, QPointF(self._origin.x(), tip_y))
        near = QColor(*state.scaled_rgb())
        near.setAlpha(int(BEAM_MAX_ALPHA * state.intensity))
        far = QColor(near)
        far.setAlpha(0)
        gradient.setColorAt(0.0, near)
        gradient.setColorAt(1.0, far)
        self.setBrush(QBrush(gradient))
        self.setVisible(True)


def _half_angle(state: FixtureState) -> Optional[float]:
    """Half-angle of the cone, or None when the fixture declares no optics.

    A live zoom channel wins over the lens' static range.
    """
    if state.zoom_deg is not None:
        return max(1.0, state.zoom_deg) / 2.0
    if state.beam_deg is not None:
        return max(1.0, sum(state.beam_deg) / 2.0) / 2.0
    return None
