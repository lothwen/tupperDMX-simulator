# ABOUTME: Renders Blinder fixtures as a stack of independently-lit bulbs.
"""Blinders.

A blinder is a housing with several bulbs that can be driven separately. How
many, and which channel feeds which, comes from the resolved state's emitters —
the n-th channel of a colour is the n-th cell — so a two-cell warm/cold blinder
and a four-cell one both work without either being special-cased here.
"""

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QGraphicsRectItem

from core.fixture_state import FixtureState
from ui.stage.base import BodyFixture, GlowingEllipse, StrobeGate
from ui.stage.registry import register_renderer

HOUSING_COLOR = QColor(20, 20, 20)
BULB_MARGIN = 5          # gap above the first bulb and below the last


@register_renderer("Blinder")
class BlinderFixture(BodyFixture):
    """A housing with one glowing bulb per emitter, stacked vertically."""

    def build_body(self) -> None:
        self.housing = QGraphicsRectItem(0, 0, self.width, self.height, self)
        self.housing.setBrush(HOUSING_COLOR)

        count = self._cell_count()
        # Share the height between the bulbs, keeping them square and centred.
        slot = (self.height - 2 * BULB_MARGIN) / count
        size = min(self.width * 0.75, slot)
        self.bulbs = []
        for index in range(count):
            top = BULB_MARGIN + index * slot + (slot - size) / 2
            self.bulbs.append(GlowingEllipse((self.width - size) / 2, top,
                                             size, size, self))
        self._strobe = StrobeGate(self._repaint)

    def _cell_count(self) -> int:
        """How many bulbs to build.

        The state's emitter count is only known once DMX arrives, so the body is
        sized from the definition: a declared cell count, else the most times any
        one colour is repeated across the channels.
        """
        declared = self.fdef.stage.get("cells")
        if isinstance(declared, int) and declared > 0:
            return declared
        repeats = {}
        for channel in self.fdef.channels:
            for capability in channel.capabilities:
                if capability.type == "ColorIntensity":
                    colour = str(capability.get("color", ""))
                    repeats[colour] = repeats.get(colour, 0) + 1
                    break
        return max(repeats.values(), default=1)

    def intensity_factor(self) -> float:
        """Blinders are drawn overdriven.

        `multiply_factor` (2.0 on the patched blinders) pushes the mix past full
        so a half-open bulb already saturates — blinders read as glare, not as a
        colour. It is a per-instance look rather than something the fixture
        emits, so it is applied here and nowhere else; every other renderer
        draws its fixture at the level the DMX asks for.
        """
        multiplier = getattr(self.instance, "multiply_factor", 1.0) or 1.0
        return self.state.intensity * float(multiplier)

    def apply_state(self, state: FixtureState) -> None:
        self._strobe.apply_state(state)
        self._repaint()

    def _repaint(self) -> None:
        factor = self.intensity_factor() * self._strobe.factor()
        for index, bulb in enumerate(self.bulbs):
            # More bulbs than the state drives: the extras stay dark.
            rgb = (self.state.emitters[index].rgb
                   if index < len(self.state.emitters) else (0, 0, 0))
            bulb.setGlowColor(self.clamped_color(rgb, factor), min(1.0, factor))

    def cleanup(self) -> None:
        self._strobe.stop()
