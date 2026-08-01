# ABOUTME: Clip library panel — a search box, a grid of group-selection pills,
# ABOUTME: and a card grid of draggable MIDI-note / scene / chase clips,
# ABOUTME: grouped data-driven from the patched fixtures' library definitions.

import json
from collections import defaultdict
from typing import Dict, List, Tuple

from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
                             QLineEdit, QToolButton, QButtonGroup, QScrollArea,
                             QLayout, QLayoutItem, QApplication, QSizePolicy)
from PyQt6.QtGui import QColor, QPainter, QDrag
from PyQt6.QtCore import Qt, QMimeData, QSize, QRect, QPoint, pyqtSignal

from core import note_layout
from core.config import Config
from core.fixture_library import GROUP_COLORS
from core.fixture_library import FixtureDef, color_slots, resolve_fixture
from software_controller.scene_loader import Scene, load_scenes
from tupperlogger import get_logger
from ui import icons


# Custom MIME type carries a JSON payload {"note": int, "velocity": int}.
# Plain-text fallback is also set so the payload is visible in tools that
# only inspect text/plain (handy for debugging the drop chain).
CLIP_MIME_TYPE = "application/x-tupperdmx-midi-clip"
DEFAULT_DROP_VELOCITY = 127

# MIME type for scene drops. The payload is a JSON object with the scene's
# name, per-channel value dict and owning fixture type; on drop the timeline
# expands it into one channel-control note_on per channel, sharing a group_id.
SCENE_MIME_TYPE = "application/x-tupperdmx-led-scene"

# Per-fixture group colours (the pill dot). Indexed by the fixture's position in
# the patch; every group belonging to a fixture — its channel page, each scene
# bank and its chases — shares that fixture's colour. The global Show group uses
# the neutral colour below.
# Shared with the timeline track headers so a fixture's swatch matches its
# library section (see core.fixture_library.GROUP_COLORS).
_GROUP_COLORS = GROUP_COLORS
_SHOW_COLOR = '#888888'

# Fixed pill size — long labels elide and reveal the full name on hover, the
# same as the clip cards. Width keeps the pill grid two-up at the default panel
# width.
_PILL_WIDTH = 114
_PILL_HEIGHT = 28


class FlowLayout(QLayout):
    """A layout that lays items left-to-right and wraps to the next row when it
    runs out of width (the standard Qt FlowLayout example). Used for the pill
    bar and the clip-card grid so both reflow with the panel width."""

    def __init__(self, parent=None, margin=0, spacing=6):
        super().__init__(parent)
        if parent is not None:
            self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)
        self._items: List[QLayoutItem] = []

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect, test_only):
        m = self.contentsMargins()
        effective = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x = effective.x()
        y = effective.y()
        line_height = 0
        spacing = self.spacing()
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + spacing
            if next_x - spacing > effective.right() and line_height > 0:
                x = effective.x()
                y = y + line_height + spacing
                next_x = x + hint.width() + spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + m.bottom()


class ClipPill(QFrame):
    """A group-selection chip: a per-fixture colour dot, the group name and a
    clip-count badge. Toggling its ``selected`` property drives the QSS
    highlight. The inert ``+ New`` tile is a pill with ``is_new=True``."""

    clicked = pyqtSignal(int)

    def __init__(self, group_index: int, label: str, color, count: int,
                 is_new: bool = False, parent=None):
        super().__init__(parent)
        self._index = group_index
        self._color = QColor(color) if color else None
        self._is_new = is_new
        self._full_label = label
        self._badge = None
        self.setObjectName("ClipPillNew" if is_new else "ClipPill")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setProperty("selected", "false")
        self.setFixedHeight(_PILL_HEIGHT)
        if not is_new:
            self.setFixedWidth(_PILL_WIDTH)

        row = QHBoxLayout(self)
        # Left margin reserves room for the painted colour dot.
        row.setContentsMargins(12 if is_new else 22, 2, 8, 2)
        row.setSpacing(6)

        self._label = QLabel(label)
        self._label.setObjectName("ClipPillLabel")
        row.addWidget(self._label)
        if not is_new:
            # Pin the count badge to the right edge; the label elides into the
            # space between the dot and the badge.
            row.addStretch(1)
            self._badge = QLabel(str(count))
            self._badge.setObjectName("ClipPillCount")
            self._badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row.addWidget(self._badge)
            self.setCursor(Qt.CursorShape.PointingHandCursor)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_elision()

    def _apply_elision(self):
        """Elide the label to fit the fixed width; show the full name as a
        tooltip only when it's actually truncated."""
        if self._is_new:
            return
        fm = self._label.fontMetrics()
        badge_w = self._badge.sizeHint().width() if self._badge is not None else 0
        avail = max(10, self.width() - 22 - 8 - badge_w - 8)
        elided = fm.elidedText(self._full_label, Qt.TextElideMode.ElideRight,
                               avail)
        self._label.setText(elided)
        tip = self._full_label if elided != self._full_label else ""
        self.setToolTip(tip)
        self._label.setToolTip(tip)
        if self._badge is not None:
            self._badge.setToolTip(tip)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._is_new or self._color is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(self._color)
        p.setPen(Qt.PenStyle.NoPen)
        d = 10
        p.drawEllipse(9, (self.height() - d) // 2, d, d)
        p.end()

    def set_selected(self, selected: bool):
        # String property values so QSS [selected="true"] matches reliably.
        self.setProperty("selected", "true" if selected else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        # Repolish the label too so its bold/regular font-weight refreshes
        # (font is resolved at the widget's own polish, not the parent's).
        self.style().unpolish(self._label)
        self.style().polish(self._label)
        # Bold (selected) text is wider than regular — re-elide for the new font.
        self._apply_elision()

    def mouseReleaseEvent(self, event):
        if (not self._is_new
                and event.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(event.position().toPoint())):
            self.clicked.emit(self._index)
        super().mouseReleaseEvent(event)


class ClipCard(QFrame):
    """A single draggable clip rendered as a card: a left accent box in the
    clip's own colour, a title and a clip-type subtitle. Carries the drag
    payload and the precomputed preview messages, so a click previews and a
    drag drops with no per-card lookups (matching the old list-item behaviour).
    """

    clicked = pyqtSignal(list)  # preview messages: [(channel, note, velocity)]

    def __init__(self, clip: Dict, parent=None):
        super().__init__(parent)
        self.setObjectName("ClipCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._accent = QColor(clip["accent"])
        self._sentinel = bool(clip.get("sentinel", False))
        self._mime_type = clip["mime_type"]
        self._payload = clip["payload"]
        self._preview = clip["preview"]
        self._press_pos = None
        self._title_text = clip["title"]
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        # Fixed width keeps the grid uniform (two columns); long titles elide.
        self.setFixedWidth(108)

        col = QVBoxLayout(self)
        # Left margin reserves room for the painted accent box.
        col.setContentsMargins(16, 6, 8, 6)
        col.setSpacing(1)
        self.title = QLabel(self._title_text)
        self.title.setObjectName("ClipCardTitle")
        self.ctype = QLabel(clip["type"])
        self.ctype.setObjectName("ClipCardType")
        col.addWidget(self.title)
        col.addWidget(self.ctype)

    def sizeHint(self):
        return QSize(108, 46)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        fm = self.title.fontMetrics()
        avail = max(10, self.width() - 16 - 8 - 2)
        elided = fm.elidedText(self._title_text, Qt.TextElideMode.ElideRight,
                               avail)
        self.title.setText(elided)
        # Reveal the full title on hover only when it's actually truncated.
        tip = self._title_text if elided != self._title_text else ""
        self.setToolTip(tip)
        self.title.setToolTip(tip)
        self.ctype.setToolTip(tip)

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(self._accent)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(6, 8, 5, self.height() - 16, 2, 2)
        if self._sentinel:
            # Scenes whose RGB channels are all the 255 palette sentinel get a
            # small star, because they trigger the controller's live-colour
            # passthrough (carried over from the old swatch).
            p.setPen(QColor(255, 255, 255))
            font = p.font()
            font.setPointSize(7)
            font.setBold(True)
            p.setFont(font)
            p.drawText(4, 15, "*")
        p.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._press_pos is None:
            return
        moved = (event.position().toPoint() - self._press_pos).manhattanLength()
        if moved < QApplication.startDragDistance():
            return
        self._press_pos = None
        self._start_drag()

    def mouseReleaseEvent(self, event):
        if (self._press_pos is not None
                and event.button() == Qt.MouseButton.LeftButton
                and self._preview):
            self.clicked.emit(self._preview)
        self._press_pos = None
        super().mouseReleaseEvent(event)

    def _start_drag(self):
        encoded = json.dumps(self._payload).encode("utf-8")
        mime = QMimeData()
        mime.setData(self._mime_type, encoded)
        mime.setText(json.dumps(self._payload))
        drag = QDrag(self)
        drag.setMimeData(mime)
        pixmap = self.grab()
        drag.setPixmap(pixmap)
        drag.setHotSpot(QPoint(pixmap.width() // 2, pixmap.height() // 2))
        drag.exec(Qt.DropAction.CopyAction)


class ClipLibraryWidget(QWidget):
    """Panel listing draggable clips, grouped from the patched fixtures' defs:
    a search box, a pill bar selecting one group, and a card grid for it."""

    # Emitted when a card is clicked. Payload is a list of
    # (channel, note, velocity) tuples — length 1 for a single clip, length N
    # for a scene expanded into its fixture's channel-control notes. The MIDI
    # channel selects the fixture (its patch index), mirroring handle_note_on.
    preview_requested = pyqtSignal(list)

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.setObjectName("ClipLibrary")
        self.config = config
        self.logger = get_logger("ui.clip_library")
        self._groups: List[Dict] = []
        self._pills: List[ClipPill] = []
        self._selected = -1

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        root.addWidget(self._build_search_row())

        self.pill_bar = QWidget()
        self.pill_bar.setObjectName("ClipPillBar")
        self.pill_flow = FlowLayout(self.pill_bar, margin=0, spacing=6)
        root.addWidget(self.pill_bar)

        root.addWidget(self._build_header_row())

        self.card_scroll = QScrollArea()
        self.card_scroll.setObjectName("ClipCardScroll")
        self.card_scroll.setWidgetResizable(True)
        self.card_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.card_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.card_grid = QWidget()
        self.card_grid.setObjectName("ClipCardGrid")
        self.card_flow = FlowLayout(self.card_grid, margin=0, spacing=6)
        self.card_scroll.setWidget(self.card_grid)
        root.addWidget(self.card_scroll, 1)

        # Match the DMX display panel's width policy so the splitter behaves
        # the same regardless of which tab is active. No maximum: the splitter
        # drives the width and the panel resizes freely.
        self.setMinimumWidth(0)

        self._rebuild()

    # ------------------------------------------------------------------
    # Static chrome
    # ------------------------------------------------------------------
    def _build_search_row(self) -> QWidget:
        row = QWidget()
        row.setObjectName("ClipSearchRow")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.search_box = QLineEdit()
        self.search_box.setObjectName("ClipSearchBox")
        self.search_box.setPlaceholderText("Search cues…")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self._on_search_changed)
        layout.addWidget(self.search_box, 1)

        # Grid / list view toggles — static visuals only (no behaviour yet).
        self._view_group = QButtonGroup(self)
        self._view_group.setExclusive(True)
        for idx, icon_name in enumerate(("grid-view", "list-view")):
            btn = QToolButton()
            btn.setObjectName("ClipViewToggle")
            btn.setIcon(icons.icon(icon_name, "#aaaaaa", on_color="#cfe9f5"))
            btn.setIconSize(QSize(13, 13))
            btn.setCheckable(True)
            btn.setChecked(idx == 0)
            self._view_group.addButton(btn)
            layout.addWidget(btn)
        return row

    def _build_header_row(self) -> QWidget:
        row = QWidget()
        row.setObjectName("ClipHeaderRow")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(4)

        self.group_header = QLabel("")
        self.group_header.setObjectName("ClipGroupHeader")
        layout.addWidget(self.group_header, 1)

        # Sort control — static visual only (no behaviour yet).
        self.sort_label = QLabel(
            f'Name {icons.img_tag("chevron-down", "#888888", 10)}')
        self.sort_label.setObjectName("ClipSortLabel")
        layout.addWidget(self.sort_label)
        return row

    # ------------------------------------------------------------------
    # Group model
    # ------------------------------------------------------------------
    def _patched_types(self) -> List[Tuple[str, str]]:
        """Unique patched (fixture type, mode) pairs, deduped, ordered by DMX
        address (matching the DMX-sorted timeline).

        Mode is part of the identity: the same type in two modes exposes
        different channels, so each needs its own group of clips."""
        seen: List[Tuple[str, str]] = []
        for inst in sorted(self.config.fixtures, key=lambda f: f.dmx_start):
            t = getattr(inst, "type", None)
            if t and (t, getattr(inst, "mode", "")) not in seen:
                seen.append((t, getattr(inst, "mode", "")))
        return seen

    def _fixture_index_for_type(self, ftype: str, mode: str = "") -> int:
        """Stable MIDI channel of the first patched fixture of ``ftype`` in
        ``mode`` (by DMX address, matching the displayed group). The channel
        selects which fixture handle_note_on targets; defaults to 0 if the type
        isn't patched."""
        for inst in sorted(self.config.fixtures, key=lambda f: f.dmx_start):
            if (getattr(inst, "type", None) == ftype
                    and getattr(inst, "mode", "") == mode):
                return getattr(inst, "channel", 0)
        return 0

    def _build_groups(self) -> List[Dict]:
        """Ordered group descriptors ``{label, color, clips}``: per patched
        fixture a channel-controls group, one group per scene bank and a chases
        group, all sharing the fixture's colour, plus a global Show group."""
        groups: List[Dict] = []
        for i, (ftype, mode) in enumerate(self._patched_types()):
            fdef = resolve_fixture(ftype, mode)
            if fdef is None:
                continue
            color = _GROUP_COLORS[i % len(_GROUP_COLORS)]
            channel = self._fixture_index_for_type(ftype, mode)

            clips = self._fixture_clips(fdef, channel)
            if clips:
                groups.append({"label": ftype.replace("_", " ").capitalize(),
                               "color": color, "clips": clips})
            for bank, bank_clips in self._bank_groups(ftype, mode, fdef, channel):
                groups.append({"label": bank.replace("_", " ").capitalize(),
                               "color": color, "clips": bank_clips})
            chase_clips = self._chase_clips(fdef, channel)
            if chase_clips:
                groups.append({"label": "Chases", "color": color,
                               "clips": chase_clips})

        show_clips = self._show_clips()
        if show_clips:
            groups.append({"label": "Show", "color": _SHOW_COLOR,
                           "clips": show_clips})
        return groups

    def _fixture_clips(self, fdef: FixtureDef, channel: int) -> List[Dict]:
        """A fixture's individual verbs: channel controls + per-fixture blackout
        + colour macros."""
        notes: List[int] = list(range(1, fdef.channel_count + 1))
        notes.append(note_layout.BLACKOUT_FIXTURE)
        notes += list(range(note_layout.COLOR_MACRO_BASE,
                            note_layout.COLOR_MACRO_BASE
                            + len(note_layout.COLOR_MACROS)))
        clips: List[Dict] = []
        for note in notes:
            info = note_layout.describe_note(note, fdef)
            if info is None:
                continue
            clips.append(self._note_clip(info, note, DEFAULT_DROP_VELOCITY,
                                         channel, self._clip_type(note)))
        return clips

    def _bank_groups(self, ftype: str, mode: str, fdef: FixtureDef, channel: int
                     ) -> List[Tuple[str, List[Dict]]]:
        """``(bank_name, [clip, ...])`` per scene bank of the fixture."""
        by_bank: Dict[str, List[Scene]] = defaultdict(list)
        for scene in load_scenes(ftype, mode):
            by_bank[scene.bank].append(scene)
        out: List[Tuple[str, List[Dict]]] = []
        for bank in sorted(by_bank.keys()):
            clips = [self._scene_clip(fdef, scene, channel)
                     for scene in sorted(by_bank[bank], key=lambda s: s.index)]
            out.append((bank, clips))
        return out

    def _chase_clips(self, fdef: FixtureDef, channel: int) -> List[Dict]:
        """The fixture's chases (drop as note CHASE_LOAD, velocity = index)."""
        clips: List[Dict] = []
        for idx in range(len(fdef.chases)):
            info = note_layout.describe_note(note_layout.CHASE_LOAD, fdef, idx)
            clips.append(self._note_clip(info, note_layout.CHASE_LOAD, idx,
                                         channel, "chase"))
        return clips

    def _show_clips(self) -> List[Dict]:
        """Show-control verbs that route globally (no fixture): START, the song
        parts and blackout-all."""
        notes = ([note_layout.START]
                 + list(range(note_layout.SONG_PART_BASE,
                              note_layout.SONG_PART_BASE
                              + len(note_layout.SONG_PARTS)))
                 + [note_layout.BLACKOUT_ALL])
        clips: List[Dict] = []
        for note in notes:
            info = note_layout.describe_note(note)
            if info is None:
                continue
            clips.append(self._note_clip(info, note, DEFAULT_DROP_VELOCITY, 0,
                                         self._clip_type(note)))
        return clips

    # ------------------------------------------------------------------
    # Clip descriptors
    # ------------------------------------------------------------------
    @staticmethod
    def _clip_type(note: int) -> str:
        """Short clip-type subtitle for a single-note clip."""
        n = int(note)
        if n in (note_layout.BLACKOUT_FIXTURE, note_layout.BLACKOUT_ALL):
            return "blackout"
        last_macro = (note_layout.COLOR_MACRO_BASE
                      + len(note_layout.COLOR_MACROS) - 1)
        if note_layout.COLOR_MACRO_BASE <= n <= last_macro:
            return "color"
        if n == note_layout.START or note_layout.is_song_part(n):
            return "cue"
        if n == note_layout.CHASE_LOAD:
            return "chase"
        return "channel"

    @staticmethod
    def _note_clip(info, note: int, velocity: int, channel: int,
                   ctype: str) -> Dict:
        payload = {"note": int(note), "velocity": int(velocity),
                   "channel": int(channel)}
        return {"title": info.part, "type": ctype, "accent": info.color,
                "mime_type": CLIP_MIME_TYPE, "payload": payload,
                "preview": [(int(channel), int(note), int(velocity))],
                "sentinel": False}

    def _scene_clip(self, fdef: FixtureDef, scene: Scene, channel: int) -> Dict:
        r, g, b = note_layout.scene_rgb(fdef, scene.values)
        preview: List[Tuple[int, int, int]] = []
        for ch_name, note in note_layout.field_note_map(fdef):
            if ch_name not in scene.values:
                continue
            # Same lossy 0..255 -> 0..127 mapping the controller doubles back.
            velocity = max(0, min(127, int(scene.values[ch_name]) // 2))
            preview.append((channel, note, velocity))
        return {"title": scene.name, "type": "scene",
                "accent": f"#{r:02x}{g:02x}{b:02x}",
                "mime_type": SCENE_MIME_TYPE,
                "payload": self._scene_to_payload(scene),
                "preview": preview,
                "sentinel": self._is_palette_sentinel(fdef, scene.values)}

    @staticmethod
    def _scene_to_payload(scene: Scene) -> Dict:
        return {
            "name": scene.name,
            "channels": dict(scene.values),
            "fixture_type": scene.fixture_type,
        }

    @staticmethod
    def _is_palette_sentinel(fdef: FixtureDef, values: Dict[str, int]) -> bool:
        """Whether a scene hands its colour to the live palette — the same
        R/G/B-at-full rule `DMXController.load_scene` applies."""
        slots = color_slots(fdef)
        fields = [fdef.channels[slots[colour]].field
                  for colour in ("Red", "Green", "Blue") if colour in slots]
        return len(fields) == 3 and all(int(values.get(f, 0)) >= 254
                                        for f in fields)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def _rebuild(self) -> None:
        self._groups = self._build_groups()
        self._render_pills()
        if self._groups:
            self._select_group(0)
        else:
            self._show_placeholder()
        self.logger.info(
            f"Clip library populated: {len(self._groups)} group(s)")

    def _render_pills(self) -> None:
        self._clear_layout(self.pill_flow)
        self._pills = []
        for i, group in enumerate(self._groups):
            pill = ClipPill(i, group["label"], group["color"],
                            len(group["clips"]))
            pill.clicked.connect(self._select_group)
            self.pill_flow.addWidget(pill)
            self._pills.append(pill)
        # Inert "+ New" tile, matching the mockup.
        self.pill_flow.addWidget(ClipPill(-1, "+ New", None, 0, is_new=True))

    def _select_group(self, index: int) -> None:
        if not (0 <= index < len(self._groups)):
            return
        self._selected = index
        # Selecting a category exits search mode (search spans all groups).
        self.search_box.blockSignals(True)
        self.search_box.clear()
        self.search_box.blockSignals(False)
        self._highlight_pill(index)
        self._render_cards()

    def _highlight_pill(self, index: int) -> None:
        for i, pill in enumerate(self._pills):
            pill.set_selected(i == index)

    def _render_cards(self) -> None:
        self._clear_layout(self.card_flow)
        query = self.search_box.text().strip().lower()
        if query:
            # Search spans every group's clips, not just the selected category.
            clips = [clip for group in self._groups
                     for clip in group["clips"]
                     if query in clip["title"].lower()]
            self.group_header.setText(f"SEARCH · {len(clips)} clips")
        elif 0 <= self._selected < len(self._groups):
            group = self._groups[self._selected]
            clips = group["clips"]
            self.group_header.setText(
                f"{group['label'].upper()} · {len(clips)} clips")
        else:
            self.group_header.setText("")
            return
        for clip in clips:
            card = ClipCard(clip)
            card.clicked.connect(self.preview_requested.emit)
            self.card_flow.addWidget(card)

    def _show_placeholder(self) -> None:
        self._selected = -1
        self.group_header.setText("")
        self._clear_layout(self.card_flow)
        placeholder = QLabel(
            "No fixtures patched.\n"
            "Patch a fixture (Design stage) or edit the fixture defs.")
        placeholder.setObjectName("ClipLibraryPlaceholder")
        placeholder.setWordWrap(True)
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder.setSizePolicy(QSizePolicy.Policy.Expanding,
                                  QSizePolicy.Policy.Expanding)
        self.card_flow.addWidget(placeholder)

    @staticmethod
    def _clear_layout(layout: QLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------
    def refresh_configuration(self, config: Config) -> None:
        """Refresh the library when configuration changes."""
        self.config = config
        self.search_box.clear()
        self._rebuild()
        self.logger.info("Clip library refreshed with new configuration")

    def _on_search_changed(self, _text: str) -> None:
        if not self._groups:
            return  # keep the "no fixtures patched" placeholder intact
        # Deselect category pills while a query is active; restore on clear.
        self._highlight_pill(-1 if self.search_box.text().strip()
                             else self._selected)
        self._render_cards()
