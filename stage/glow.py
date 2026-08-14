# ABOUTME: The glowing lamp primitive and the falloff curves it fades with.
"""How a lamp's halo is painted.

`GlowingEllipse` is the bulb every emission reaches for: an ellipse that paints
its own radial-gradient glow. The two profiles beside it are the falloffs it
fades with — the classic one every fixture has used since before there were
beams, and the lens shape that agrees with the shader's own POINT falloff.
"""

import math

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QGraphicsEllipseItem
from PyQt6.QtGui import QBrush, QColor, QPainter, QRadialGradient

#: Peak alpha at the centre of a halo, before the lamp's own intensity.
GLOW_PEAK_ALPHA = 180

#: The classic falloff, as (position, alpha): every glowing thing on this stage
#: has faded this way since before there were beams — a wide flat core out to an
#: eighth, then roughly (1-t)² to nothing at the rim. The numbers are the ones
#: that were written out stop by stop, so nothing on the stage moves.
SHARP_GLOW = ((0.0, 180), (0.13, 130), (0.25, 90), (0.5, 40), (1.0, 0))


def _diffuse_glow(stops=14):
    """A tight core in a long, soft halo — the shape of a lens, not a ball.

    The flat core and steep shoulder above are what give a small halo a findable
    edge: past the shoulder there is almost nothing left, so the eye reads the
    shoulder as the rim of a disc. This is the GPU's own POINT falloff instead
    (`shaders.LIGHT_FRAG`: an inverse-square halo with a hot core, windowed out
    at the quad's edge), sampled into gradient stops — so the raster lens and
    the shader's agree on more than they used to, and the halo fades over its
    whole radius rather than ending somewhere inside it.
    """
    def alpha(t):
        halo = 1.0 / (1.0 + 24.0 * t * t)
        core = 0.6 * math.exp(-40.0 * t * t)
        window = 1.0 - _smoothstep(0.75, 1.0, t)
        # 1.6 is the value at t = 0, so the core is GLOW_PEAK_ALPHA like the
        # sharp profile's and only the shape around it differs.
        return GLOW_PEAK_ALPHA * (halo + core) * window / 1.6

    return tuple((index / stops, alpha(index / stops))
                 for index in range(stops + 1))


def _smoothstep(low, high, t):
    x = max(0.0, min(1.0, (t - low) / (high - low)))
    return x * x * (3.0 - 2.0 * x)


DIFFUSE_GLOW = _diffuse_glow()


class GlowingEllipse(QGraphicsEllipseItem):
    """Custom QGraphicsEllipseItem with shader-like radial gradient glow effect"""

    def __init__(self, x, y, width, height, parent=None):
        super().__init__(x, y, width, height, parent)
        self.glow_color = QColor("darkgrey")
        self.glow_intensity = 0.0
        self.glow_radius_multiplier = 20.0  # How far the glow extends beyond the ellipse

    #: False once the GPU layer owns the light: the bulb still draws, so an
    #: unlit fixture looks like itself, but the glow is left to the shader
    #: rather than painted twice — once alpha-stacked here, once added there.
    draw_glow = True

    #: Which falloff this lamp fades with. A lens sets it: its halo is small
    #: enough that the classic curve's shoulder reads as the edge of a ball.
    glow_profile = SHARP_GLOW

    #: Whether an unlit lamp paints its own stand-in face. True everywhere by
    #: default — for most fixtures this flat disc IS the dark bulb. A profile
    #: that draws its own glass turns it off, or the stand-in covers the glass
    #: with a lighter, flatter circle than the housing it sits in.
    draw_unlit_face = True

    def setGlowColor(self, color, intensity=1.0):
        """Set the glow color and intensity (0.0-1.0)"""
        self.glow_color = color
        self.glow_intensity = max(0.0, min(1.0, intensity))
        self.update()  # Trigger repaint

    def set_glow_radius(self, multiplier):
        """Resize the halo. The one path that may change it after construction.

        A moving head's lens blooms as its beam turns toward the viewer, so this
        moves between frames — and it is the bounding rect, which Qt caches.
        Without `prepareGeometryChange` a halo that shrank left its old self
        painted on the scene until something else happened to repaint that
        patch.
        """
        if abs(multiplier - self.glow_radius_multiplier) < 1e-3:
            return
        self.prepareGeometryChange()
        self.glow_radius_multiplier = multiplier
        self.update()


    def boundingRect(self):
        """Expand bounding rect to include glow area"""
        rect = super().boundingRect()
        glow_expansion = self.rect().width() * (self.glow_radius_multiplier - 1) / 2
        return rect.adjusted(-glow_expansion, -glow_expansion, glow_expansion, glow_expansion)
        
    def paint(self, painter, option, widget):
        """Custom paint method with radial gradient glow"""
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        ellipse_rect = self.rect()
        center_x = ellipse_rect.center().x()
        center_y = ellipse_rect.center().y()
        bulb_radius = ellipse_rect.width() / 2
        glow_radius = bulb_radius * self.glow_radius_multiplier
        
        if self.glow_intensity > 0 and not self.draw_glow:
            # The GPU layer owns the halo, but this is still the lit face of
            # the lamp — drawing it dark would switch the fixture off.
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(self.glow_color))
            painter.drawEllipse(ellipse_rect)
        elif self.glow_intensity > 0:

            # Create smooth radial gradient for seamless glow
            gradient = QRadialGradient(center_x, center_y, glow_radius)
            for position, alpha in self.glow_profile:
                colour = QColor(self.glow_color)
                colour.setAlpha(int(alpha * self.glow_intensity))
                gradient.setColorAt(position, colour)

            # Paint the entire glow with smooth gradient
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(gradient))
            glow_diameter = int(glow_radius * 2)
            painter.drawEllipse(
                int(center_x - glow_radius), int(center_y - glow_radius),
                glow_diameter, glow_diameter
            )
        elif self.draw_unlit_face:
            # No glow - just dark grey bulb
            painter.setBrush(QBrush(QColor(40, 40, 40)))
            painter.setPen(self.pen())
            painter.drawEllipse(ellipse_rect)
