import glob
from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtWidgets import QGraphicsRectItem, QGraphicsDropShadowEffect, QGraphicsObject, QStyle, QGraphicsEllipseItem
from PyQt6.QtGui import QColor, QBrush, QMovie, QPainter, QRadialGradient


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
            #gradient.setColorAt(0.0, QColor("red"))
            
            # Inner bright area
            inner_color = QColor(self.glow_color)
            inner_color.setAlpha(int(180 * self.glow_intensity))
            gradient.setColorAt(0.0, inner_color)
            #gradient.setColorAt(0.06, QColor("blue"))
            
            # Mid glow
            mid_color = QColor(self.glow_color)
            mid_color.setAlpha(int(130 * self.glow_intensity))
            gradient.setColorAt(0.13, mid_color)
            #gradient.setColorAt(0.13, QColor("green"))
            
            # Outer glow
            outer_color = QColor(self.glow_color)
            outer_color.setAlpha(int(90 * self.glow_intensity))
            gradient.setColorAt(0.25, outer_color)
            #gradient.setColorAt(0.25, QColor("yellow"))
            
            # Far glow
            far_color = QColor(self.glow_color)
            far_color.setAlpha(int(40 * self.glow_intensity))
            gradient.setColorAt(0.5, far_color)
            #gradient.setColorAt(0.5, QColor("white"))
            
            # Edge - fade to transparent
            edge_color = QColor(self.glow_color)
            edge_color.setAlpha(0)
            gradient.setColorAt(1.0, edge_color)
            #gradient.setColorAt(1.0, QColor("purple"))
            
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
            


class LedBar(QGraphicsObject):
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

    def boundingRect(self):
        return QRectF(0, 0, self.WIDTH, self.HEIGHT)

    def paint(self, painter, option, widget):
        #option.state = QStyle.StateFlag.State_None
        #return super(LedBar, self).paint(painter,option)
        pass
        
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
            gif_path_list = glob.glob(f"assets/animations/{animation_number:02d}-*.gif")
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

class Blinder(QGraphicsObject):
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
        
        # Drop shadow glow effect
        #dse = QGraphicsDropShadowEffect()
        #dse.setBlurRadius(200)
        #dse.setOffset(0)
        #self.setGraphicsEffect(dse)

    def boundingRect(self):
        return QRectF(0, 0, self.WIDTH, self.HEIGHT)

    def paint(self, painter, option, widget):
        # Let child items handle their own painting
        pass

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
        
        r1 = min(255, r1) * brightness_factor
        g1 = min(255, g1) * brightness_factor
        b1 = min(255, b1) * brightness_factor
        
        bulb1_color = QColor(int(r1), int(g1), int(b1))
        self.bulb1.setGlowColor(bulb1_color, brightness_factor)

        # Update bottom bulb (LED 2)
        led2_warm_intensity = self.led2_warm / 255
        led2_cool_intensity = self.led2_cool / 255
        
        r2 = warm_color.red() * led2_warm_intensity + cool_color.red() * led2_cool_intensity
        g2 = warm_color.green() * led2_warm_intensity + cool_color.green() * led2_cool_intensity
        b2 = warm_color.blue() * led2_warm_intensity + cool_color.blue() * led2_cool_intensity
        
        r2 = min(255, r2) * brightness_factor
        g2 = min(255, g2) * brightness_factor  
        b2 = min(255, b2) * brightness_factor
        
        bulb2_color = QColor(int(r2), int(g2), int(b2))
        self.bulb2.setGlowColor(bulb2_color, brightness_factor)
        """
        # Update glow effect with combined color
        avg_r = (r1 + r2) / 2
        avg_g = (g1 + g2) / 2
        avg_b = (b1 + b2) / 2
        
        glow_color = QColor(int(avg_r), int(avg_g), int(avg_b))
        self.graphicsEffect().setColor(glow_color)
        """
        
