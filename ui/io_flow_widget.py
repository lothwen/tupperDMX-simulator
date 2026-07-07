# ABOUTME: IOFlowWidget — custom-painted live signal-flow graph for the Settings
# ABOUTME: I/O page: nodes/edges rendered from io_routing.FlowState, marching-ants dashes.

import math

from PyQt6.QtWidgets import QWidget
from PyQt6.QtGui import QPainter, QColor, QFont, QPen, QPainterPath, QBrush, QPolygonF
from PyQt6.QtCore import Qt, QRectF, QPointF, QSize, QTimer

import io_routing
from io_routing import (STATUS_ACTIVE, STATUS_DIM, STATUS_DEAD,
                        NODE_SEQ, NODE_MIDI_PORT, NODE_CTRL, NODE_ARTNET,
                        NODE_USB, NODE_STAGE, EDGE_SEQ_CTRL, EDGE_SEQ_PORT,
                        EDGE_CTRL_STAGE, EDGE_CTRL_ARTNET, EDGE_CTRL_USB,
                        EDGE_ARTNET_STAGE, EDGE_USB_STAGE, MIDI_EDGES)

# --- palette (painted in code; QSS can't reach a custom paintEvent) ----------
COL_MIDI = QColor("#c586d6")            # MIDI edges — pink-violet, per mockups
COL_DMX = QColor("#e6a23c")             # DMX edges — amber
COL_DEAD = QColor("#e05252")           # broken route — red
COL_DIM = QColor("#5a5a5a")            # unused route — grey
COL_NODE_BG = QColor("#1f1f1f")
COL_NODE_BORDER = QColor("#3a3a3a")
COL_NODE_BORDER_ACTIVE = QColor("#4a90e2")
COL_TITLE = QColor("#e0e0e0")
COL_TITLE_DIM = QColor("#7a7a7a")
COL_SUB = QColor("#8a8a8a")
COL_PILL_BG = QColor("#c0392b")
COL_BADGE = QColor("#2f6db0")           # numbered badge — matches the Settings cards

# Static node titles; the dynamic second line comes from FlowState.subtitles.
NODE_TITLES = {
    NODE_SEQ: "Sequencer",
    NODE_MIDI_PORT: "MIDI Port",
    NODE_CTRL: "Internal ctrl",
    NODE_ARTNET: "Art-Net",
    NODE_USB: "USB DMX",
    NODE_STAGE: "Virtual stage",
}

# Nodes that carry a circled number, drawn as a blue badge (like the left cards).
NODE_BADGES = {NODE_SEQ: "1", NODE_CTRL: "2", NODE_STAGE: "3"}

# All edges + whether their destination arrow sits at the node's top / left /
# right edge (used to place the arrowhead cleanly against the target node).
ALL_EDGES = (EDGE_SEQ_CTRL, EDGE_SEQ_PORT, EDGE_CTRL_STAGE, EDGE_CTRL_ARTNET,
             EDGE_CTRL_USB, EDGE_ARTNET_STAGE, EDGE_USB_STAGE)

LEGEND_H = 26


class IOFlowWidget(QWidget):
    """Render-only live signal-flow graph. Feed it an io_routing.FlowState via
    set_state(); active edges animate a marching-ants dash while the widget is
    shown."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = None
        self._dash_offset = 0.0
        self._dash_timer = QTimer(self)
        self._dash_timer.setInterval(33)  # ~30 fps
        self._dash_timer.timeout.connect(self._advance_dash)

    # --- public API -----------------------------------------------------------

    def set_state(self, state: io_routing.FlowState) -> None:
        self._state = state
        self.update()

    def minimumSizeHint(self) -> QSize:
        return QSize(340, 400)

    # --- animation lifecycle --------------------------------------------------

    def showEvent(self, event):
        super().showEvent(event)
        self._dash_timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._dash_timer.stop()

    def _advance_dash(self):
        self._dash_offset = (self._dash_offset + 0.8) % 10.0
        self.update()

    # --- layout ---------------------------------------------------------------

    def _node_rects(self):
        """Node rectangles as a function of the current widget size (below the
        legend strip). Returns {node_id: QRectF}."""
        w = self.width()
        h = self.height()
        top = LEGEND_H
        area_h = h - top
        nw = max(130.0, min(180.0, w * 0.40))
        nh = 46.0
        left_x = w * 0.05
        right_x = w - nw - w * 0.05

        def rc(x, yf):
            return QRectF(x, top + area_h * yf, nw, nh)

        return {
            NODE_SEQ: rc(left_x, 0.04),
            NODE_MIDI_PORT: rc(right_x, 0.04),
            NODE_CTRL: rc(left_x, 0.40),
            NODE_ARTNET: rc(right_x, 0.34),
            NODE_USB: rc(right_x, 0.60),
            NODE_STAGE: rc(left_x, 0.86),
        }

    def _edge_path(self, edge, rects) -> QPainterPath:
        """A QPainterPath for one edge and the unit direction of its arrowhead."""
        src = rects[edge[0]]
        dst = rects[edge[1]]
        path = QPainterPath()

        if edge == EDGE_SEQ_CTRL:
            a = QPointF(src.center().x(), src.bottom())
            b = QPointF(dst.center().x(), dst.top())
            path.moveTo(a)
            path.lineTo(b)
        elif edge == EDGE_SEQ_PORT:
            a = QPointF(src.right(), src.center().y())
            b = QPointF(dst.left(), dst.center().y())
            path.moveTo(a)
            path.lineTo(b)
        elif edge == EDGE_CTRL_STAGE:
            a = QPointF(src.center().x(), src.bottom())
            b = QPointF(dst.center().x(), dst.top())
            path.moveTo(a)
            path.lineTo(b)
        elif edge in (EDGE_CTRL_ARTNET, EDGE_CTRL_USB):
            a = QPointF(src.right(), src.center().y())
            b = QPointF(dst.left(), dst.center().y())
            mx = (a.x() + b.x()) / 2
            path.moveTo(a)
            path.cubicTo(QPointF(mx, a.y()), QPointF(mx, b.y()), b)
        elif edge in (EDGE_ARTNET_STAGE, EDGE_USB_STAGE):
            # Swing out through the right margin and return into the stage's
            # right edge.
            a = QPointF(src.right(), src.center().y())
            b = QPointF(dst.right(), dst.center().y())
            out_x = max(a.x(), b.x()) + (self.width() - max(a.x(), b.x())) * 0.5
            path.moveTo(a)
            path.cubicTo(QPointF(out_x, a.y()), QPointF(out_x, b.y()), b)
        else:
            path.moveTo(src.center())
            path.lineTo(dst.center())
        return path

    # --- paint ----------------------------------------------------------------

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._state is None:
            p.end()
            return

        self._draw_legend(p)

        rects = self._node_rects()
        # Edges first, so nodes sit on top of the line ends.
        for edge in ALL_EDGES:
            status = self._state.edges.get(edge, STATUS_DIM)
            self._draw_edge(p, edge, rects, status, edge in MIDI_EDGES)
        for node_id, rect in rects.items():
            status = self._state.nodes.get(node_id, STATUS_DIM)
            self._draw_node(p, node_id, rect, status)
        p.end()

    def _draw_legend(self, p: QPainter):
        w = self.width()
        y = LEGEND_H / 2
        font = QFont()
        font.setPointSize(8)
        p.setFont(font)
        fm = p.fontMetrics()
        # Right-aligned: [dash] MIDI  [dash] DMX
        dmx_text_w = fm.horizontalAdvance("DMX")
        midi_text_w = fm.horizontalAdvance("MIDI")
        seg = 18
        gap = 5
        x = w - 6
        # DMX (rightmost)
        x -= dmx_text_w
        p.setPen(QColor(COL_SUB))
        p.drawText(QRectF(x, 0, dmx_text_w, LEGEND_H),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "DMX")
        x -= gap + seg
        self._dashed_swatch(p, x, y, seg, COL_DMX)
        x -= 16
        # MIDI
        x -= midi_text_w
        p.setPen(QColor(COL_SUB))
        p.drawText(QRectF(x, 0, midi_text_w, LEGEND_H),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "MIDI")
        x -= gap + seg
        self._dashed_swatch(p, x, y, seg, COL_MIDI)

    def _dashed_swatch(self, p: QPainter, x, y, length, color):
        pen = QPen(color, 2)
        pen.setDashPattern([4, 3])
        p.setPen(pen)
        p.drawLine(QPointF(x, y), QPointF(x + length, y))

    def _draw_edge(self, p: QPainter, edge, rects, status, is_midi):
        path = self._edge_path(edge, rects)
        if status == STATUS_ACTIVE:
            color = COL_MIDI if is_midi else COL_DMX
            pen = QPen(color, 2)
            pen.setDashPattern([6, 4])
            pen.setDashOffset(-self._dash_offset)
        elif status == STATUS_DEAD:
            pen = QPen(COL_DEAD, 2)
            pen.setDashPattern([6, 4])
        else:
            pen = QPen(COL_DIM, 1)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

        # Arrowhead at the destination end, oriented along the final segment.
        if status != STATUS_DIM:
            end = path.pointAtPercent(1.0)
            near = path.pointAtPercent(0.92)
            self._draw_arrowhead(p, near, end,
                                 COL_DEAD if status == STATUS_DEAD
                                 else (COL_MIDI if is_midi else COL_DMX))

    def _draw_arrowhead(self, p: QPainter, frm: QPointF, to: QPointF, color):
        ang = math.atan2(to.y() - frm.y(), to.x() - frm.x())
        size = 12.0
        p1 = to
        p2 = QPointF(to.x() - size * math.cos(ang - math.pi / 7),
                     to.y() - size * math.sin(ang - math.pi / 7))
        p3 = QPointF(to.x() - size * math.cos(ang + math.pi / 7),
                     to.y() - size * math.sin(ang + math.pi / 7))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(color))
        p.drawPolygon(QPolygonF([p1, p2, p3]))

    def _draw_node(self, p: QPainter, node_id, rect: QRectF, status):
        path = QPainterPath()
        path.addRoundedRect(rect, 4, 4)
        p.fillPath(path, COL_NODE_BG)

        if status == STATUS_ACTIVE:
            border, bw = COL_NODE_BORDER_ACTIVE, 2
        elif status == STATUS_DEAD:
            border, bw = COL_DEAD, 2
        else:
            border, bw = COL_NODE_BORDER, 1
        p.setPen(QPen(border, bw))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

        dim = status == STATUS_DIM
        title_font = QFont()
        title_font.setPointSize(10)
        title_font.setBold(True)
        p.setFont(title_font)
        p.setPen(COL_TITLE_DIM if dim else COL_TITLE)
        p.drawText(rect.adjusted(8, 5, -8, -rect.height() / 2),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   NODE_TITLES.get(node_id, node_id))

        sub_font = QFont()
        sub_font.setPointSize(8)
        p.setFont(sub_font)
        p.setPen(COL_SUB)
        subtitle = self._state.subtitles.get(node_id, "")
        p.drawText(rect.adjusted(8, rect.height() / 2, -8, -5),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   subtitle)

        # Circled number as a blue badge pinned to the node's top-left corner.
        number = NODE_BADGES.get(node_id)
        if number:
            self._draw_number_badge(p, QPointF(rect.left() + 13, rect.top() + 13),
                                    number)

        # BLACKOUT pill on the stage node's top-right corner when it's dead.
        if node_id == NODE_STAGE and status == STATUS_DEAD:
            self._draw_blackout_pill(p, rect)

    def _draw_number_badge(self, p: QPainter, center: QPointF, text: str):
        r = 9.0
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(COL_BADGE))
        p.drawEllipse(center, r, r)
        font = QFont()
        font.setPointSize(8)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor("#ffffff"))
        p.drawText(QRectF(center.x() - r, center.y() - r, 2 * r, 2 * r),
                   Qt.AlignmentFlag.AlignCenter, text)

    def _draw_blackout_pill(self, p: QPainter, node_rect: QRectF):
        font = QFont()
        font.setPointSize(7)
        font.setBold(True)
        p.setFont(font)
        text = "BLACKOUT"
        tw = p.fontMetrics().horizontalAdvance(text) + 12
        pill = QRectF(node_rect.right() - tw + 6, node_rect.top() - 9, tw, 16)
        path = QPainterPath()
        path.addRoundedRect(pill, 8, 8)
        p.fillPath(path, COL_PILL_BG)
        p.setPen(QColor("#ffffff"))
        p.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)
