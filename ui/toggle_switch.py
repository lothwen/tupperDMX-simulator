# ABOUTME: ToggleSwitch — an iOS-style sliding toggle widget shared by the
# ABOUTME: settings dialog and the timeline toolbar. A QCheckBox in behavior
# ABOUTME: (isChecked/setChecked/toggled), custom-painted track + animated knob.

from PyQt6.QtWidgets import QCheckBox
from PyQt6.QtCore import Qt, QVariantAnimation, QEasingCurve, QRectF, QPointF
from PyQt6.QtGui import QPainter, QColor, QBrush


class ToggleSwitch(QCheckBox):
    """An iOS-style sliding toggle. Stays a QCheckBox so isChecked()/setChecked()
    and the ``toggled`` signal work exactly like a checkbox; only the painting
    and the click target change. The knob animation is driven from the state
    hooks rather than the ``toggled`` signal: checkStateSet() covers programmatic
    setChecked() (incl. under blockSignals()), and nextCheckState() covers user
    clicks — a click routes through QAbstractButton::click(), which suppresses
    checkStateSet() (blockRefresh), so without the latter hook the knob would
    never move on click."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(44, 24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pos = 1.0 if self.isChecked() else 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(120)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._anim.valueChanged.connect(self._on_anim)

    def _on_anim(self, value):
        self._pos = float(value)
        self.update()

    def _animate_to_checked(self):
        target = 1.0 if self.isChecked() else 0.0
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(target)
        self._anim.start()

    def checkStateSet(self):
        super().checkStateSet()
        self._animate_to_checked()

    def nextCheckState(self):
        super().nextCheckState()
        self._animate_to_checked()

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        enabled = self.isEnabled()

        r = QRectF(1, 1, self.width() - 2, self.height() - 2)
        if self.isChecked():
            track = QColor("#2f6db0")
        else:
            track = QColor("#555555")
        if not enabled:
            track = track.darker(160)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(track))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)

        d = r.height() - 4
        x = r.left() + 2 + self._pos * (r.width() - d - 4)
        knob = QColor("#f0f0f0") if enabled else QColor("#999999")
        p.setBrush(QBrush(knob))
        p.drawEllipse(QPointF(x + d / 2, r.center().y()), d / 2, d / 2)
        p.end()
