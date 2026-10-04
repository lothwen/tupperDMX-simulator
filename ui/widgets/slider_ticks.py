# ABOUTME: SliderTickLabels — the names of a stepped slider's stops, written
# ABOUTME: under the ticks they name. Shared by the settings dialog's Quality
# ABOUTME: slider and the auto program dialog's Feel slider.

from PyQt6.QtCore import QEvent, QSize, Qt
from PyQt6.QtWidgets import (QLabel, QSizePolicy, QSlider, QStyle, QStyleOptionSlider,
                             QWidget)


class SliderTickLabels(QWidget):
    """The names of a slider's stops, written under the ticks they name.

    Positioned against the slider's own geometry rather than by even stretch:
    a groove is inset by half a handle at each end, so equal spacing would put
    the first and last names a few pixels off the ticks they belong to. The
    names at the ends are clamped into the strip so they stay readable rather
    than being clipped by it.
    """

    def __init__(self, slider: QSlider, names, parent=None):
        super().__init__(parent)
        self._slider = slider
        self._labels = []
        for name in names:
            label = QLabel(name, self)
            label.setObjectName("SliderTickLabel")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._labels.append(label)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)
        self.set_active(slider.value())

    def set_active(self, index: int) -> None:
        """Light up the name the handle is sitting on."""
        for position, label in enumerate(self._labels):
            active = "true" if position == index else "false"
            if label.property("active") != active:
                label.setProperty("active", active)
                label.style().unpolish(label)
                label.style().polish(label)

    def active_name(self) -> str:
        """The name currently lit — the readout, as it is on screen."""
        return next((label.text() for label in self._labels
                     if label.property("active") == "true"), "")

    def sizeHint(self) -> QSize:
        return QSize(self._slider.sizeHint().width(),
                     max(label.sizeHint().height() for label in self._labels))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        # The Interface page previews a new UI scale while this dialog is open,
        # and a restyle changes what these names measure.
        if event.type() in (QEvent.Type.StyleChange, QEvent.Type.FontChange):
            self.updateGeometry()
            self._place()

    def _place(self) -> None:
        slider = self._slider
        option = QStyleOptionSlider()
        slider.initStyleOption(option)
        groove = slider.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, option,
            QStyle.SubControl.SC_SliderGroove, slider)
        handle = slider.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, option,
            QStyle.SubControl.SC_SliderHandle, slider)
        span = groove.width() - handle.width()

        for value, label in enumerate(self._labels):
            offset = slider.style().sliderPositionFromValue(
                slider.minimum(), slider.maximum(), value, span)
            centre = groove.x() + offset + handle.width() / 2
            width = label.sizeHint().width()
            left = min(max(0, round(centre - width / 2)),
                       max(0, self.width() - width))
            label.setGeometry(left, 0, width, self.height())
