import glob
import os
from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtWidgets import QGraphicsRectItem, QGraphicsDropShadowEffect, QGraphicsObject, QStyle, QGraphicsEllipseItem, QGraphicsItem
from PyQt6.QtGui import QColor, QBrush, QMovie, QPainter, QRadialGradient, QPen, QFont, QFontMetrics

from core.paths import asset_path
from core.fixture_library import (
    ROLE_BRIGHTNESS, ROLE_RED, ROLE_GREEN, ROLE_BLUE,
    ROLE_WHITE_WARM, ROLE_WHITE_COOL,
)


class FixtureLabel(QGraphicsItem):
    """Constant-size name + DMX chip shown under a fixture in edit mode.

    Uses ItemIgnoresTransformations so it stays a fixed on-screen size (and
    legible) regardless of the stage's fit scale. Lives as a top-level scene
    item rather than a child of the fixture, so the LED bar's drop-shadow glow
    effect doesn't blur it; its position is kept in sync by the fixture's
    itemChange hook."""

    _PAD_X = 6
    _PAD_Y = 3
    _GAP = 6  # gap below the fixture's bottom edge, in device pixels

    def __init__(self):
        super().__init__()
        self._text = ""
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setZValue(100)
        self._font = QFont()
        self._font.setPixelSize(11)

    def set_text(self, text: str):
        if text != self._text:
            self.prepareGeometryChange()
            self._text = text
            self.update()

    def _box(self) -> QRectF:
        fm = QFontMetrics(self._font)
        w = fm.horizontalAdvance(self._text) + 2 * self._PAD_X
        h = fm.height() + 2 * self._PAD_Y
        return QRectF(-w / 2, self._GAP, w, h)  # centred under the anchor, below it

    def boundingRect(self) -> QRectF:
        return self._box()

    def paint(self, painter, option, widget):
        if not self._text:
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = self._box()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(25, 25, 25, 230))
        painter.drawRoundedRect(box, 4, 4)
        painter.setFont(self._font)
        painter.setPen(QColor(235, 235, 235))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, self._text)


class DesignableFixture:
    """Mixin adding scenario-designer behaviour to a fixture render object:
    optional move/select (gated by design mode), a position write-back on
    drag-release, an edit-mode outline, and a name/DMX label. Mixed in BEFORE
    QGraphicsObject so super() reaches Qt.

    Persistence follows the timeline's convention: position changes are visual
    during the drag and only committed on mouseRelease (via the move callback).
    """

    # Outline colours (RGB): every fixture gets a thin edit-mode border; the
    # selected one a thicker, lighter-blue border.
    _EDIT_OUTLINE = (60, 130, 230)
    _SELECT_OUTLINE = (120, 200, 255)

    def init_design(self):
        # Called from each fixture __init__ after the Qt object is constructed.
        self._instance = None          # config.FixtureInstance back-reference
        self._bar_index = None         # set for the bars of a multi-bar ledbar
        self._move_callback = None     # callable(render_obj) invoked on release
        self._design_mode = False
        self._label = None             # FixtureLabel (top-level scene item)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)

    def set_design_mode(self, enabled: bool):
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, enabled)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, enabled)
        self._design_mode = enabled
        if self._label is not None:
            self.refresh_label()
            self._label.setVisible(enabled and self._is_label_owner())
        self.update()

    # -- name/DMX label -------------------------------------------------
    def _is_label_owner(self) -> bool:
        """Only the first bar of a multi-bar ledbar carries a label (the others
        share the same instance, so they'd be duplicates)."""
        return self._bar_index in (None, 0)

    def attach_label(self, scene):
        """Create the fixture's label as a top-level scene item."""
        self._label = FixtureLabel()
        scene.addItem(self._label)
        self._update_label_pos()
        self.refresh_label()
        self._label.setVisible(self._design_mode and self._is_label_owner())

    def remove_label(self, scene):
        if self._label is not None:
            scene.removeItem(self._label)
            self._label = None

    def refresh_label(self):
        """Sync the label text with the fixture's instance (name + DMX address)."""
        if self._label is None:
            return
        inst = self._instance
        if inst is None or not self._is_label_owner():
            self._label.set_text("")
            return
        name = inst.name or inst.type
        self._label.set_text(f"{name} · {inst.dmx_start}")

    def _update_label_pos(self):
        if self._label is None:
            return
        br = self.boundingRect()
        self._label.setPos(self.pos().x() + br.center().x(),
                           self.pos().y() + br.bottom())

    def itemChange(self, change, value):
        if (change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged
                and self._label is not None):
            self._update_label_pos()
        return super().itemChange(change, value)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if (self._move_callback is not None
                and self.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable):
            self._move_callback(self)

    def paint_selection(self, painter):
        """Outline the fixture: a thin edit-mode border on every fixture, a
        thicker selection border on the selected one. Called from each fixture's
        paint() (their own paint is otherwise a no-op)."""
        if self.isSelected():
            rgb, width = self._SELECT_OUTLINE, 8
        elif self._design_mode:
            rgb, width = self._EDIT_OUTLINE, 4
        else:
            return
        pen = QPen(QColor(*rgb), width)
        pen.setCosmetic(True)  # constant on-screen width regardless of fit scale
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self.boundingRect().adjusted(1, 1, -1, -1))


class GlowingEllipse(QGraphicsEllipseItem):
    """Custom QGraphicsEllipseItem with shader-like radial gradient glow effect"""
    
    def __init__(self, x, y, width, height, parent=None):
        super().__init__(x, y, width, height, parent)
        self.glow_color = QColor("darkgrey")
        self.glow_intensity = 0.0
        self.glow_radius_multiplier = 20.0  # How far the glow extends beyond the ellipse
        
    def setGlowColor(self, color, intensity=1.0):
        """Set the glow color and intensity (0.0-1.0)"""
        self.glow_color = color
        self.glow_intensity = max(0.0, min(1.0, intensity))
        self.update()  # Trigger repaint
        
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
        
        if self.glow_intensity > 0:
            
            # Create smooth radial gradient for seamless glow
            gradient = QRadialGradient(center_x, center_y, glow_radius)
            
            # Core - full intensity
            core_color = QColor(self.glow_color)
            core_color.setAlpha(int(220 * self.glow_intensity))
            gradient.setColorAt(0.0, core_color)
            
            # Inner bright area
            inner_color = QColor(self.glow_color)
            inner_color.setAlpha(int(180 * self.glow_intensity))
            gradient.setColorAt(0.0, inner_color)
            
            # Mid glow
            mid_color = QColor(self.glow_color)
            mid_color.setAlpha(int(130 * self.glow_intensity))
            gradient.setColorAt(0.13, mid_color)
            
            # Outer glow
            outer_color = QColor(self.glow_color)
            outer_color.setAlpha(int(90 * self.glow_intensity))
            gradient.setColorAt(0.25, outer_color)
            
            # Far glow
            far_color = QColor(self.glow_color)
            far_color.setAlpha(int(40 * self.glow_intensity))
            gradient.setColorAt(0.5, far_color)
            
            # Edge - fade to transparent
            edge_color = QColor(self.glow_color)
            edge_color.setAlpha(0)
            gradient.setColorAt(1.0, edge_color)
            
            # Paint the entire glow with smooth gradient
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(gradient))
            glow_diameter = int(glow_radius * 2)
            painter.drawEllipse(
                int(center_x - glow_radius), int(center_y - glow_radius),
                glow_diameter, glow_diameter
            )
        else:
            # No glow - just dark grey bulb
            painter.setBrush(QBrush(QColor(40, 40, 40)))
            painter.setPen(self.pen())
            painter.drawEllipse(ellipse_rect)
            


class LedBar(DesignableFixture, QGraphicsObject):
    dmx_channel_count = 7
    NUM_PIXELS = 40
    WIDTH = 15
    HEIGHT = 400

    def __init__(self, x, y, index):
        super().__init__()
        self.setPos(x, y)
        self.index = index
        
        self.pixels = []
        pixel_height = self.HEIGHT / self.NUM_PIXELS
        for i in range(self.NUM_PIXELS):
            pixel = QGraphicsRectItem(0, i * pixel_height, self.WIDTH, pixel_height, self)
            pixel.setBrush(QColor("darkgrey"))
            self.pixels.append(pixel)

        self.brightness = 0
        self.red = 0
        self.green = 0
        self.blue = 0
        
        self.animations = {}
        self.current_animation_number = -1
        self.current_movie = None

        dse = QGraphicsDropShadowEffect()
        dse.setBlurRadius(100)
        dse.setOffset(0)
        self.setGraphicsEffect(dse)

        self.init_design()

    def boundingRect(self):
        return QRectF(0, 0, self.WIDTH, self.HEIGHT)

    def cleanup(self):
        """Stop the animation movie before this bar is removed from the scene.

        Disconnecting frameChanged also breaks the movie<->bar reference cycle,
        so the bar is freed promptly (by refcount) instead of lingering to a
        cyclic GC, and its animation stops ticking the moment it's deleted."""
        if self.current_movie:
            try:
                self.current_movie.frameChanged.disconnect(self.update_animation_frame)
            except TypeError:
                pass  # Signal was not connected
            self.current_movie.stop()
        self.current_movie = None
        self.animations.clear()

    def paint(self, painter, option, widget):
        #option.state = QStyle.StateFlag.State_None
        #return super(LedBar, self).paint(painter,option)
        self.paint_selection(painter)

    def update_dmx(self, data):
        if len(data) < self.dmx_channel_count:
            return

        self.brightness = data[0]
        self.red = data[3]
        self.green = data[2]
        self.blue = data[1]
        
        animation_dmx_val = data[4]
        animation_number = animation_dmx_val // 4 + 1

        if animation_number != self.current_animation_number:
            self.set_animation(animation_number)
            self.current_animation_number = animation_number

        if not (self.current_movie and self.current_movie.isValid()):
            color = QColor(
                int(self.red * self.brightness / 255),
                int(self.green * self.brightness / 255),
                int(self.blue * self.brightness / 255)
            )
            for pixel in self.pixels:
                pixel.setBrush(QBrush(color))
            
            self.graphicsEffect().setColor(QColor(self.red, self.green, self.blue, 255))

    def set_animation(self, animation_number):
        if self.current_movie:
            try:
                self.current_movie.frameChanged.disconnect(self.update_animation_frame)
            except TypeError:
                pass # Signal was not connected
            self.current_movie.stop()
            self.current_movie = None

        if animation_number in self.animations:
            self.current_movie = self.animations[animation_number]
        else:
            _assets = asset_path("animations")
            gif_path_list = glob.glob(os.path.join(_assets, f"{animation_number:02d}-*.gif"))
            if gif_path_list:
                movie = QMovie(gif_path_list[0])
                self.animations[animation_number] = movie
                self.current_movie = movie
        
        if self.current_movie:
            self.current_movie.frameChanged.connect(self.update_animation_frame)
            self.current_movie.start()

    def update_animation_frame(self):
        if not self.current_movie:
            return

        image = self.current_movie.currentImage()
        if image.isNull() or image.width() <= self.index:
            return

        total_r, total_g, total_b = 0, 0, 0

        for i in range(self.NUM_PIXELS):
            if i < image.height():
                pixel_color = image.pixelColor(self.index, i)
                
                # Modulate GIF color with DMX color
                gif_r, gif_g, gif_b = pixel_color.red(), pixel_color.green(), pixel_color.blue()
                
                new_r = int((gif_r / 255.0) * self.red)
                new_g = int((gif_g / 255.0) * self.green)
                new_b = int((gif_b / 255.0) * self.blue)

                total_r += new_r
                total_g += new_g
                total_b += new_b

                brightness_factor = self.brightness / 255.0
                final_color = QColor(
                    int(new_r * brightness_factor),
                    int(new_g * brightness_factor),
                    int(new_b * brightness_factor)
                )
                self.pixels[i].setBrush(final_color)
        
        if self.NUM_PIXELS > 0:
            avg_r = total_r / self.NUM_PIXELS
            avg_g = total_g / self.NUM_PIXELS
            avg_b = total_b / self.NUM_PIXELS
            self.graphicsEffect().setColor(QColor(int(avg_r), int(avg_g), int(avg_b), 255))

class Blinder(DesignableFixture, QGraphicsObject):
    dmx_channel_count = 8
    WIDTH = 40
    HEIGHT = 80
    BULB_SIZE = 30

    def __init__(self, x, y, multiply_factor=2.0):
        super().__init__()
        self.setPos(x, y)

        self.brightness = 0
        self.led1_cool = 0  # Top bulb cool (DMX channel 2)
        self.led2_cool = 0  # Bottom bulb cool (DMX channel 3)
        self.led1_warm = 0  # Top bulb warm (DMX channel 4)
        self.led2_warm = 0  # Bottom bulb warm (DMX channel 5)
        self.multiply_factor = multiply_factor
        
        # Create housing (black rectangle)
        self.housing = QGraphicsRectItem(0, 0, self.WIDTH, self.HEIGHT, self)
        self.housing.setBrush(QColor(20, 20, 20))  # Dark housing
        
        # Create two bulbs with custom glow effect
        bulb_x = (self.WIDTH - self.BULB_SIZE) / 2
        
        # Top bulb (LED 1)
        self.bulb1 = GlowingEllipse(
            bulb_x, 5, self.BULB_SIZE, self.BULB_SIZE, self
        )
        
        # Bottom bulb (LED 2)
        self.bulb2 = GlowingEllipse(
            bulb_x, self.HEIGHT - self.BULB_SIZE - 5, self.BULB_SIZE, self.BULB_SIZE, self
        )

        self.init_design()

    def boundingRect(self):
        return QRectF(0, 0, self.WIDTH, self.HEIGHT)

    def paint(self, painter, option, widget):
        # Child items handle the fixture itself; we only add the selection border.
        self.paint_selection(painter)

    def update_dmx(self, data):
        if len(data) < self.dmx_channel_count:
            return

        # DMX mapping:
        # Channel 1: Master brightness
        # Channel 2: LED 1 (top) cool
        # Channel 3: LED 2 (bottom) cool  
        # Channel 4: LED 1 (top) warm
        # Channel 5: LED 2 (bottom) warm
        self.brightness = data[0]
        self.led1_cool = data[1]  # Top cool
        self.led2_cool = data[2]  # Bottom cool
        self.led1_warm = data[3]  # Top warm
        self.led2_warm = data[4]  # Bottom warm

        # Color definitions
        warm_color = QColor(255, 200, 100)  # Amber
        cool_color = QColor(200, 220, 255)  # Cool white
        
        brightness_factor = (self.brightness / 255) * self.multiply_factor

        # Update top bulb (LED 1)
        led1_warm_intensity = self.led1_warm / 255
        led1_cool_intensity = self.led1_cool / 255
        
        r1 = warm_color.red() * led1_warm_intensity + cool_color.red() * led1_cool_intensity
        g1 = warm_color.green() * led1_warm_intensity + cool_color.green() * led1_cool_intensity
        b1 = warm_color.blue() * led1_warm_intensity + cool_color.blue() * led1_cool_intensity

        # Clamp after applying brightness_factor: multiply_factor can push the
        # product past 255, and QColor() with out-of-range channels is invalid
        # and renders as nothing (would make the bulb appear off).
        r1 = min(255, min(255, r1) * brightness_factor)
        g1 = min(255, min(255, g1) * brightness_factor)
        b1 = min(255, min(255, b1) * brightness_factor)

        bulb1_color = QColor(int(r1), int(g1), int(b1))
        self.bulb1.setGlowColor(bulb1_color, brightness_factor)

        # Update bottom bulb (LED 2)
        led2_warm_intensity = self.led2_warm / 255
        led2_cool_intensity = self.led2_cool / 255
        
        r2 = warm_color.red() * led2_warm_intensity + cool_color.red() * led2_cool_intensity
        g2 = warm_color.green() * led2_warm_intensity + cool_color.green() * led2_cool_intensity
        b2 = warm_color.blue() * led2_warm_intensity + cool_color.blue() * led2_cool_intensity

        r2 = min(255, min(255, r2) * brightness_factor)
        g2 = min(255, min(255, g2) * brightness_factor)
        b2 = min(255, min(255, b2) * brightness_factor)

        bulb2_color = QColor(int(r2), int(g2), int(b2))
        self.bulb2.setGlowColor(bulb2_color, brightness_factor)


class GenericFixture(DesignableFixture, QGraphicsObject):
    """Data-driven renderer for library fixture types without a bespoke class.

    Draws a single glowing cell whose colour/intensity is derived from the
    fixture's role-tagged channels (brightness + RGB and/or warm/cool white).
    ``role_index`` maps a ROLE_* int to that role's position within the DMX
    slice; ``dmx_channel_count`` is an instance attribute since it varies by type.
    """
    WIDTH = 40
    HEIGHT = 40
    BULB_SIZE = 34

    # Warm/cool white tints, matching the Blinder's palette.
    _WARM = (255, 200, 100)
    _COOL = (200, 220, 255)

    def __init__(self, x, y, channel_count, role_index):
        super().__init__()
        self.setPos(x, y)
        self.dmx_channel_count = channel_count
        self._role_index = role_index

        self.housing = QGraphicsRectItem(0, 0, self.WIDTH, self.HEIGHT, self)
        self.housing.setBrush(QColor(20, 20, 20))

        margin = (self.WIDTH - self.BULB_SIZE) / 2
        self.bulb = GlowingEllipse(margin, margin, self.BULB_SIZE, self.BULB_SIZE, self)

        self.init_design()

    def boundingRect(self):
        return QRectF(0, 0, self.WIDTH, self.HEIGHT)

    def paint(self, painter, option, widget):
        # Child items (housing + bulb) handle their own painting; we add the border.
        self.paint_selection(painter)

    def _channel(self, data, role, default=0):
        idx = self._role_index.get(role)
        if idx is None or idx >= len(data):
            return default
        return data[idx]

    def update_dmx(self, data):
        if len(data) < self.dmx_channel_count:
            return

        # Brightness defaults to full when the fixture has no brightness channel.
        has_brightness = ROLE_BRIGHTNESS in self._role_index
        brightness = self._channel(data, ROLE_BRIGHTNESS, 255) if has_brightness else 255

        r = self._channel(data, ROLE_RED)
        g = self._channel(data, ROLE_GREEN)
        b = self._channel(data, ROLE_BLUE)

        warm = self._channel(data, ROLE_WHITE_WARM)
        cool = self._channel(data, ROLE_WHITE_COOL)
        r += self._WARM[0] * warm / 255 + self._COOL[0] * cool / 255
        g += self._WARM[1] * warm / 255 + self._COOL[1] * cool / 255
        b += self._WARM[2] * warm / 255 + self._COOL[2] * cool / 255

        # A pure dimmer (no colour channels at all) glows white.
        has_color = any(role in self._role_index for role in
                        (ROLE_RED, ROLE_GREEN, ROLE_BLUE, ROLE_WHITE_WARM, ROLE_WHITE_COOL))
        if not has_color:
            r = g = b = 255

        scale = brightness / 255.0
        color = QColor(
            min(255, int(r * scale)),
            min(255, int(g * scale)),
            min(255, int(b * scale)),
        )
        self.bulb.setGlowColor(color, scale)

