# ABOUTME: Clip library panel — a search box, a grid of group-selection pills,
# ABOUTME: and a card grid of draggable MIDI-note / scene / chase clips,
# ABOUTME: grouped data-driven from the patched fixtures' library definitions.

import json
from typing import Dict, List, Optional

from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
                             QLineEdit, QToolButton, QButtonGroup, QScrollArea,
                             QLayout, QApplication, QSizePolicy, QMenu)
from PyQt6.QtGui import QColor, QPainter, QDrag, QCursor
from PyQt6.QtCore import Qt, QMimeData, QRect, QSize, QPoint, pyqtSignal

from core import clips as clip_model
from core.config import Config
from tupperlogger import get_logger
from ui.widgets import icons
from ui.widgets.chips import FlowLayout


# The favourites star drawn on each card, and the gold it lights up in. Sized
# so the 108px card keeps a readable title beside it.
_STAR_SIZE = 12
_STAR_ON_COLOR = '#f5c542'
_STAR_OFF_COLOR = '#5a5a5a'

# The Favourites category, pinned first in the pill bar.
_FAVORITES_LABEL = 'Favorites'
_FAVORITES_COLOR = _STAR_ON_COLOR
# Accent for a favourite whose clip no longer resolves — matches the web
# remote's dead-button swatch.
_UNAVAILABLE_COLOR = '#4a4a4a'

# Fixed pill size — long labels elide and reveal the full name on hover, the
# same as the clip cards. Width keeps the pill grid two-up at the default panel
# width.
_PILL_WIDTH = 114
_PILL_HEIGHT = 28


class ClipPill(QFrame):
    """A group-selection chip: a per-fixture colour dot, the group name and a
    clip-count badge. Toggling its ``selected`` property drives the QSS
    highlight. The ``+ New`` tile is a pill with ``is_new=True``: no dot, no
    badge, no elision, and it reports index -1 rather than a group."""

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
        if is_new:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip("Add scenes and chases to a fixture")
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
        # The "+ New" tile reports index -1, which is not a group — the panel
        # routes it to the fixture editor rather than to a selection.
        if (event.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(event.position().toPoint())):
            self.clicked.emit(self._index)
        super().mouseReleaseEvent(event)


class ClipCard(QFrame):
    """A single draggable clip rendered as a card: a left accent box in the
    clip's own accent colour, a title, a clip-type subtitle and a star that
    adds it to the show's favourites. Carries the drag payload and the
    precomputed preview messages, so a click previews and a drag drops with no
    per-card lookups (matching the old list-item behaviour).

    A card built from a favourite that no longer resolves is inert: no drag, no
    preview, only the star, which is how the operator clears it."""

    clicked = pyqtSignal(list)      # preview messages: [(channel, note, velocity)]
    star_toggled = pyqtSignal(dict)  # the clip's ref
    # Right-clicked: (the clip's ref, its clip type). The MENU is not built
    # here — the panel knows the patch, so it can name the fixture that would
    # open, and it is not one of the widgets a patch edit destroys.
    context_menu_requested = pyqtSignal(dict, str)

    def __init__(self, clip: Dict, starred: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("ClipCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._accent = QColor(clip["accent"])
        self._sentinel = bool(clip.get("sentinel", False))
        self._mime_type = clip.get("mime_type")
        self._payload = clip.get("payload")
        self._preview = clip.get("preview") or []
        self._ref = clip.get("ref")
        self._available = bool(clip.get("available", True))
        self._starred = bool(starred)
        self._press_pos = None
        self._press_on_star = False
        self._title_text = clip["title"]
        self._subtitle_text = clip["type"]
        self.setProperty("available", "true" if self._available else "false")
        self._base_cursor = (Qt.CursorShape.OpenHandCursor if self._available
                             else Qt.CursorShape.ArrowCursor)
        self._on_star = False
        self.setCursor(self._base_cursor)
        # Hover moves need to arrive with no button held, so the cursor can
        # change over the star.
        self.setMouseTracking(True)
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
        avail = max(10, self.width() - 16 - 8 - 2 - _STAR_SIZE - 2)
        elided = fm.elidedText(self._title_text, Qt.TextElideMode.ElideRight,
                               avail)
        self.title.setText(elided)
        # Reveal the full title on hover only when it's actually truncated.
        tip = self._title_text if elided != self._title_text else ""
        if not self._available:
            # An unresolved favourite carries its reason in the subtitle, which
            # is far too long for a 108px card — hovering is the only way to
            # read WHY the card is dead.
            tip = f"{self._title_text} — {self._subtitle_text}"
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
        # The star is painted rather than parented as a button: a child widget
        # over a draggable card swallows the press that starts the drag.
        if self._starred or self._available:
            name = "star-filled" if self._starred else "star"
            color = _STAR_ON_COLOR if self._starred else _STAR_OFF_COLOR
            p.drawPixmap(self._star_rect().topLeft(),
                         icons.pixmap(name, _STAR_SIZE, color))
        p.end()

    def _star_rect(self) -> QRect:
        return QRect(self.width() - _STAR_SIZE - 5, 5, _STAR_SIZE, _STAR_SIZE)

    def _hit_star(self, pos) -> bool:
        # Generous target: the drawn star is 12px on a 108px card.
        return self._ref is not None and \
            self._star_rect().adjusted(-4, -4, 4, 4).contains(pos)

    def mousePressEvent(self, event):
        # On the PRESS, never the release: the drag and the star are both
        # left-button-only, so a right press cannot fight either of them.
        if event.button() == Qt.MouseButton.RightButton:
            self.context_menu_requested.emit(self._ref or {},
                                             self._subtitle_text)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position().toPoint()
            self._press_on_star = self._hit_star(self._press_pos)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        self._update_cursor(pos)
        if self._press_pos is None or self._press_on_star or not self._available:
            return
        moved = (pos - self._press_pos).manhattanLength()
        if moved < QApplication.startDragDistance():
            return
        self._press_pos = None
        self._start_drag()

    def mouseReleaseEvent(self, event):
        if (self._press_pos is not None
                and event.button() == Qt.MouseButton.LeftButton):
            if self._press_on_star and self._hit_star(event.position().toPoint()):
                self.star_toggled.emit(self._ref)
            elif self._preview and self._available:
                self.clicked.emit(self._preview)
        self._press_pos = None
        self._press_on_star = False
        super().mouseReleaseEvent(event)

    def _update_cursor(self, pos) -> None:
        """The star is a button sitting inside a draggable card, so the cursor
        has to say which of the two is under the pointer. Only set on a change:
        this runs on every hover move."""
        on_star = self._hit_star(pos)
        if on_star == self._on_star:
            return
        self._on_star = on_star
        self.setCursor(Qt.CursorShape.PointingHandCursor if on_star
                       else self._base_cursor)

    def leaveEvent(self, event):
        self._on_star = False
        self.setCursor(self._base_cursor)
        super().leaveEvent(event)

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
    # A card's menu or the "+ New" tile wants a patched fixture opened for
    # editing: (its stable MIDI channel, which tab to land on, and the clip's
    # own ref so the editor can open ON that scene or chase rather than merely
    # on its tab). The ref is empty when nothing in particular was asked for.
    edit_fixture_requested = pyqtSignal(int, str, dict)

    def __init__(self, config: Config, show_manager=None, parent=None):
        super().__init__(parent)
        self.setObjectName("ClipLibrary")
        self.config = config
        # Favourites are show content, so the show owns them; without a show
        # manager (standalone/tests) the panel simply has no star state.
        self.show_manager = show_manager
        self.logger = get_logger("ui.clip_library")
        self._groups: List[Dict] = []
        self._pills: List[ClipPill] = []
        self._starred_ids: set = set()
        self._selected = -1
        if show_manager is not None:
            show_manager.favorites_changed.connect(self._on_favorites_changed)

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        root.addWidget(self._build_search_row())

        self.pill_bar = QWidget()
        self.pill_bar.setObjectName("ClipPillBar")
        self.pill_flow = FlowLayout(self.pill_bar, spacing=6)
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
        self.card_flow = FlowLayout(self.card_grid, spacing=6)
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

    def _build_groups(self) -> List[Dict]:
        """The patch's groups, led by the show's Favourites when it has any.

        Resolution happens ONCE here: the resolved descriptors are the very
        objects the other groups hold, so a starred card is marked by identity
        rather than by re-deriving the match card by card."""
        groups = clip_model.build_groups(self.config)
        favorites = (self.show_manager.favorites()
                     if self.show_manager is not None else [])
        resolved = clip_model.resolve_in(groups, favorites)
        self._starred_ids = {id(clip) for clip in resolved if clip is not None}
        if not favorites:
            return groups
        clips = [clip if clip is not None else self._unavailable_clip(ref)
                 for ref, clip in zip(favorites, resolved)]
        return [{"label": _FAVORITES_LABEL, "color": _FAVORITES_COLOR,
                 "clips": clips}] + groups

    @staticmethod
    def _unavailable_clip(ref: Dict) -> Dict:
        """Placeholder card for a favourite that no longer resolves. It is kept
        on screen — never quietly dropped — because repatching the fixture
        brings the real clip back, and the star is how the operator clears it
        deliberately."""
        title, reason = clip_model.describe_ref(ref)
        return {"title": title, "type": reason, "accent": _UNAVAILABLE_COLOR,
                "sentinel": False, "ref": ref, "available": False}

    def _render_pills(self) -> None:
        self._clear_layout(self.pill_flow)
        self._pills = []
        for i, group in enumerate(self._groups):
            pill = ClipPill(i, group["label"], group["color"],
                            len(group["clips"]))
            pill.clicked.connect(self._select_group)
            self.pill_flow.addWidget(pill)
            self._pills.append(pill)
        # "+ New" tile: index -1, so its click routes to the fixture editor
        # rather than to _select_group.
        new_pill = ClipPill(-1, "+ New", None, 0, is_new=True)
        new_pill.clicked.connect(lambda _index: self._on_new_clicked())
        self.pill_flow.addWidget(new_pill)

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
            card = ClipCard(clip, starred=self._is_starred(clip))
            card.clicked.connect(self.preview_requested.emit)
            card.star_toggled.connect(self._on_star_toggled)
            card.context_menu_requested.connect(self._on_card_menu)
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
    def _is_starred(self, clip: Dict) -> bool:
        # Identity, not ref equality: _build_groups already resolved the show's
        # favourites onto these exact descriptors. A card in the Favourites
        # group is starred by definition, resolved or not.
        return (id(clip) in self._starred_ids
                or not clip.get("available", True))

    def _on_star_toggled(self, ref: Dict) -> None:
        if self.show_manager is None or not ref:
            return
        self.show_manager.toggle_favorite(ref)

    # --- opening the fixture editor -----------------------------------
    def _instance_for_type(self, ftype: str, mode: str = ""):
        """The patched instance a (type, mode) group speaks for, or None.

        The first by DMX address, which is the one `fixture_index_for_type`
        picks and so the one the group's own clips already fire at. When the
        same type is patched twice this is an arbitrary-but-consistent choice,
        which is why the menu names the fixture it will open.
        """
        candidates = [f for f in self.config.fixtures
                      if f.type == ftype
                      and (not mode or getattr(f, "mode", "") == mode)]
        if not candidates:
            # A group's mode came from the patch, so a miss here means the type
            # is patched in another mode only — still the right fixture to edit.
            candidates = [f for f in self.config.fixtures if f.type == ftype]
        if not candidates:
            return None
        return min(candidates, key=lambda f: getattr(f, "dmx_start", 0))

    def _instance_for_ref(self, ref: Dict):
        """The patched instance a clip's ref points at, or None for one that
        names no fixture — a global Show verb, or a favourite whose fixture has
        been unpatched."""
        if not ref:
            return None
        ftype = ref.get("fixture")
        if not ftype:
            return None
        return self._instance_for_type(ftype, ref.get("mode", "") or "")

    @staticmethod
    def _instance_label(instance) -> str:
        return instance.name or instance.type

    def _ask_to_edit(self, instance, clip_type: str = "",
                     ref: Optional[Dict] = None) -> None:
        """Announce the request. A chase card lands on the Chases tab; anything
        else on Scenes — and `ref`, when a card supplied one, says which scene
        or chase on it."""
        channel = int(getattr(instance, "channel", -1))
        if channel < 0:
            return
        tab = "chases" if clip_type == "chase" else "scenes"
        self.edit_fixture_requested.emit(channel, tab, dict(ref or {}))

    def _on_card_menu(self, ref: Dict, clip_type: str) -> None:
        """Right-click on a card. The menu is built here rather than on the
        card so it can name the instance that will actually open — and so its
        owner is not one of the widgets a patch edit destroys."""
        instance = self._instance_for_ref(ref)
        if instance is None:
            return  # a Show verb, or a favourite that no longer resolves
        menu = QMenu(self)
        edit = menu.addAction(f"Edit {self._instance_label(instance)}…")
        if menu.exec(QCursor.pos()) is edit:
            self._ask_to_edit(instance, clip_type, ref)

    def _selected_group_instance(self):
        """The fixture behind the selected pill, or None when no single one
        owns the view: search mode (which spans every group), the global Show
        group, or the synthetic Favourites group (mixed fixtures)."""
        if self.search_box.text().strip():
            return None
        if not (0 <= self._selected < len(self._groups)):
            return None
        group = self._groups[self._selected]
        ftype = group.get("fixture")
        if not ftype or group["label"] == _FAVORITES_LABEL:
            return None
        return self._instance_for_type(ftype, group.get("mode", "") or "")

    def _on_new_clicked(self) -> None:
        """The "+ New" tile. New scenes and chases are authored in the fixture
        editor, so this opens it — on the fixture the view is already about
        when there is one, and otherwise on the one the operator picks."""
        instance = self._selected_group_instance()
        if instance is not None:
            self._ask_to_edit(instance)
            return
        self._pick_fixture_to_edit()

    def _pick_fixture_to_edit(self) -> None:
        """Show / Favourites / search: no one fixture owns the view, so ask."""
        patched = sorted(self.config.fixtures,
                         key=lambda f: getattr(f, "dmx_start", 0))
        if not patched:
            return  # the placeholder already says why
        if len(patched) == 1:
            self._ask_to_edit(patched[0])
            return
        menu = QMenu(self)
        actions = {menu.addAction(
            f"{self._instance_label(f)}  ·  ch {getattr(f, 'channel', -1)}"): f
            for f in patched}
        chosen = menu.exec(QCursor.pos())
        if chosen in actions:
            self._ask_to_edit(actions[chosen])

    def _on_favorites_changed(self) -> None:
        """Rebuild for a star change, keeping the operator where they were —
        the Favourites pill appears and disappears, so the selection is
        restored by NAME rather than by index."""
        label = (self._groups[self._selected]["label"]
                 if 0 <= self._selected < len(self._groups) else None)
        query = self.search_box.text()
        self._groups = self._build_groups()
        self._render_pills()
        index = next((i for i, g in enumerate(self._groups)
                      if g["label"] == label), 0)
        if self._groups:
            self._select_group(index)
            if query:
                self.search_box.setText(query)
        else:
            self._show_placeholder()

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
