import glob
from PyQt6.QtCore import QRectF
from PyQt6.QtWidgets import QGraphicsRectItem, QGraphicsDropShadowEffect, QGraphicsObject, QStyle
from PyQt6.QtGui import QColor, QBrush, QMovie


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

class Blinder(QGraphicsRectItem):
    dmx_channel_count = 8

    def __init__(self, x, y):
        super().__init__(0, 0, 50, 50)
        self.setPos(x, y)
        self.setBrush(QColor("darkgrey"))

        self.brightness = 0
        self.led1_cool = 0
        self.led2_cool = 0
        self.led1_warm = 0
        self.led2_warm = 0
        
        dse = QGraphicsDropShadowEffect();
        dse.setBlurRadius(100);
        dse.setOffset(0);
        self.setGraphicsEffect(dse);

    def update_dmx(self, data):
        if len(data) < self.dmx_channel_count:
            return
        # DMX: Brightness, led 1 Cool, led 2 Cool, led 1 Warm, led 2  Warm
        self.brightness = data[0]
        self.led1_cool = data[1]
        self.led2_cool = data[2]
        self.led1_warm = data[3]
        self.led2_warm = data[4]

        # More realistic color mix
        warm_color = QColor(255, 180, 100)  # Amber
        cool_color = QColor(200, 200, 255)  # Light Blue/White

        warm_intensity = (self.led1_warm + self.led2_warm) / 2 / 255
        cool_intensity = (self.led1_cool + self.led2_cool) / 2 / 255

        # Blend colors
        r = warm_color.red() * warm_intensity + cool_color.red() * cool_intensity
        g = warm_color.green() * warm_intensity + cool_color.green() * cool_intensity
        b = warm_color.blue() * warm_intensity + cool_color.blue() * cool_intensity

        # Clamp values
        r = min(255, r)
        g = min(255, g)
        b = min(255, b)

        # Apply master brightness
        brightness_factor = self.brightness / 255
        color = QColor(
            int(r * brightness_factor),
            int(g * brightness_factor),
            int(b * brightness_factor)
        )
        self.setBrush(QBrush(color))
        self.graphicsEffect().setColor(QColor(int(r), int(g), int(b), 255))
        
