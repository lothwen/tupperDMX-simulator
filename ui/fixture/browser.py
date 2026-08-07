# ABOUTME: The "Add fixture" browser — a searchable library tree (grouped by
# ABOUTME: manufacturer or category) over an OFL detail view, plus a patch footer.
"""Fixture picker for the stage designer.

Everything shown here is read straight from the fixture library: the tree groups
by *manufacturer* (the folder a definition lives in, see
`core.fixture_library._load_dir`) or by *category* (the OFL `categories` array,
where a fixture legitimately belongs to several groups at once). The detail
panel renders the OFL document — physical data, metadata, links, the channel
layout of the selected mode, and every mode the fixture declares — plus the
Scenes tab (`ui/fixture_scenes_tab.py`), which edits the type's sidecar scene
banks rather than the patch.

The footer is the actual patch decision: where in the universe the fixture lands
and which MIDI channel drives it. Both are validated live against the current
patch, and OK stays disabled until the choice is legal.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import QRect, QRectF, Qt, QUrl
from PyQt6.QtGui import (
    QBrush, QColor, QDesktopServices, QFont, QFontMetrics, QPainter,
)
from PyQt6.QtWidgets import (
    QButtonGroup, QDialog, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPushButton, QRadioButton, QScrollArea, QSizePolicy, QSpinBox,
    QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTabWidget, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core import ofl
from core.fixture_library import (
    FixtureDef, fixture_document, load_fixture_defs, resolve_fixture,
)
from core.channel_families import channel_family as _channel_family
from ui.widgets.chips import FlowLayout, chip as _chip
from ui.fixture.scenes_tab import FixtureScenesTab

UNIVERSE_SIZE = 512

# Group name for definitions sitting directly in a fixtures dir, i.e. with no
# manufacturer folder — the shipped ledbar/blinder/par_rgb and anything a user
# drops in loose.
GENERIC_MANUFACTURER = "Generic"

GROUP_BY_MANUFACTURER = "manufacturer"
GROUP_BY_CATEGORY = "category"

# The channel-family table lives in core/channel_families.py and the chip label
# in ui/fixture_chips.py — the Scenes tab colours its faders by the same
# families, and so does the timeline.

_CATEGORY_COLOR = "#79cdf0"


# --- small formatting helpers -------------------------------------------------

def _number(value: float) -> str:
    """Trim the trailing ".0" off whole numbers ("600 W", not "600.0 W")."""
    return f"{value:g}"


def _capability_summary(cap: ofl.Capability) -> str:
    """One capability as a line of prose, preferring what the author wrote."""
    text = (cap.get("comment") or cap.get("effectName") or cap.get("effectPreset")
            or cap.get("shutterEffect") or cap.get("color") or cap.get("wheel"))
    if not text and isinstance(cap.get("colors"), list):
        text = ", ".join(str(c) for c in cap.get("colors"))
    return str(text) if text else cap.type


def _physical_rows(physical: ofl.OflPhysical) -> List[Tuple[str, str]]:
    """The PHYSICAL card's rows, skipping whatever the document doesn't state."""
    rows: List[Tuple[str, str]] = []
    if physical.dimensions_mm:
        rows.append(("Dimensions",
                     " × ".join(_number(d) for d in physical.dimensions_mm) + " mm"))
    if physical.weight_kg:
        rows.append(("Weight", f"{_number(physical.weight_kg)} kg"))
    if physical.power_w:
        rows.append(("Power", f"{_number(physical.power_w)} W"))
    if physical.power_connectors:
        rows.append(("Power connector",
                     ", ".join(f"{name} ({direction})"
                               for name, direction in physical.power_connectors.items())))
    if physical.dmx_connector:
        rows.append(("DMX connector", physical.dmx_connector))

    bulb = [physical.bulb_type]
    if physical.bulb_color_temp_k:
        bulb.append(f"{_number(physical.bulb_color_temp_k)} K")
    if physical.bulb_lumens:
        bulb.append(f"{_number(physical.bulb_lumens)} lm")
    bulb = [part for part in bulb if part]
    if bulb:
        rows.append(("Bulb", " · ".join(bulb)))

    lens = [physical.lens_name]
    if physical.lens_degrees:
        low, high = physical.lens_degrees
        lens.append(f"{_number(low)}…{_number(high)}°")
    lens = [part for part in lens if part]
    if lens:
        rows.append(("Lens", " · ".join(lens)))
    return rows


def _metadata_rows(document: ofl.OflFixture) -> List[Tuple[str, str]]:
    """The METADATA card's rows: who wrote this definition, and when."""
    meta = document.meta
    rows: List[Tuple[str, str]] = []
    if document.short_name and document.short_name != document.name:
        rows.append(("Short name", document.short_name))
    if meta.authors:
        rows.append(("Authors", ", ".join(meta.authors)))
    if meta.create_date:
        rows.append(("Created", meta.create_date))
    if meta.last_modify_date:
        rows.append(("Last modified", meta.last_modify_date))
    if meta.import_plugin:
        rows.append(("Import plugin", meta.import_plugin.plugin))
    rows.append(("RDM", f"model {document.rdm.model_id}" if document.rdm
                 else "not supported"))
    return rows


# --- patch helpers ------------------------------------------------------------
#
# Shared with the designer's inspector, which validates the same two things when
# an already-patched fixture is edited.

def suggest_free_dmx_start(config) -> int:
    """First DMX address past the currently patched channels (1 if empty)."""
    used = config.get_used_dmx_channels()
    highest = 0
    for channels in used.values():
        if channels:
            highest = max(highest, max(channels))
    return min(UNIVERSE_SIZE, highest + 1) if highest else 1


def used_midi_channels(config, exclude=None) -> set:
    """MIDI channels in use by patched fixtures, excluding `exclude`."""
    return {f.channel for f in config.fixtures
            if f is not exclude and f.channel >= 0}


def used_dmx_spans(config) -> List[Tuple[str, int, int]]:
    """Patched (name, first, last) DMX spans, 1-based and inclusive."""
    spans: List[Tuple[str, int, int]] = []
    for name, channels in config.get_used_dmx_channels().items():
        if channels:
            spans.append((name, min(channels), max(channels)))
    return spans


# --- widgets ------------------------------------------------------------------

class _TreeAsideDelegate(QStyledItemDelegate):
    """The library tree's second column: a badge on a group, a caption on a leaf.

    A group's member count sits in a dark pill so it reads as a tally rather
    than as more text, and a fixture's manufacturer/category is set a size
    smaller and dimmer than its name so the eye lands on the name first.
    Neither is expressible in the stylesheet — QSS reaches a tree's items but
    not one column of them — so this paints the column and leaves the row's own
    background, selection and hover to the QSS as before.
    """

    _BADGE_BG = QColor("#1B1D1E")
    _BADGE_BORDER = QColor("#1B1D1E")
    _BADGE_TEXT = QColor("#8f8f8f")
    _CAPTION = QColor("#6f6f6f")
    # On the blue selection band the dim grey would sink; lift it just enough.
    _CAPTION_SELECTED = QColor("#cfe0ee")

    _MARGIN = 8          # right-hand breathing room, matching the tree's padding

    def paint(self, painter, option, index):
        style_option = QStyleOptionViewItem(option)
        self.initStyleOption(style_option, index)
        text = style_option.text
        # Draw the row (its QSS background, selection, hover) with no text, then
        # put ours in its place.
        style_option.text = ""
        widget = style_option.widget
        widget.style().drawControl(QStyle.ControlElement.CE_ItemViewItem,
                                   style_option, painter, widget)
        if not text:
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = _smaller_font(style_option.font)
        painter.setFont(font)
        if index.parent().isValid():
            self._paint_caption(painter, style_option, font, text)
        else:
            self._paint_badge(painter, style_option, font, text)
        painter.restore()

    def _paint_badge(self, painter, option, font: QFont, text: str) -> None:
        height = min(option.rect.height() - 4, 17)
        width = max(height, QFontMetrics(font).horizontalAdvance(text) + 11)
        rect = QRectF(option.rect.right() - width - self._MARGIN,
                      option.rect.center().y() - height / 2 + 1, width, height)
        painter.setPen(self._BADGE_BORDER)
        painter.setBrush(self._BADGE_BG)
        painter.drawRoundedRect(rect, height / 2, height / 2)
        painter.setPen(self._BADGE_TEXT)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_caption(self, painter, option, font: QFont, text: str) -> None:
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.setPen(self._CAPTION_SELECTED if selected else self._CAPTION)
        rect = option.rect.adjusted(0, 0, -self._MARGIN, 0)
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignRight
                                   | Qt.AlignmentFlag.AlignVCenter),
                         QFontMetrics(font).elidedText(
                             text, Qt.TextElideMode.ElideRight, rect.width()))


def _smaller_font(font: QFont) -> QFont:
    """`font` one step down and never bold — QSS sizes the tree in pixels, but
    a point-sized font has to keep working too."""
    smaller = QFont(font)
    if font.pixelSize() > 0:
        smaller.setPixelSize(max(8, font.pixelSize() - 2))
    else:
        smaller.setPointSizeF(max(6.0, font.pointSizeF() - 1.5))
    smaller.setBold(False)
    return smaller


class PatchBar(QWidget):
    """The universe at a glance: hatched blocks for what is patched, a solid one
    for where the new fixture would land (red if the two collide)."""

    _BORDER = QColor("#3f3f3f")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(26)
        self.setMinimumWidth(180)
        self._used: List[Tuple[int, int]] = []
        self._pending: Optional[Tuple[int, int]] = None
        self._conflict = False

    def set_spans(self, used: List[Tuple[int, int]],
                  pending: Optional[Tuple[int, int]], conflict: bool) -> None:
        self._used, self._pending, self._conflict = used, pending, conflict
        self.update()

    def _inner(self) -> QRect:
        """The universe itself — the frame is drawn around it, not over it."""
        return self.rect().adjusted(1, 1, -1, -1)

    def _span_rect(self, first: int, last: int) -> QRect:
        inner = self._inner()
        scale = inner.width() / UNIVERSE_SIZE
        left = inner.left() + int((first - 1) * scale)
        right = min(inner.right() + 1,
                    max(left + 2, inner.left() + int(last * scale)))
        return QRect(left, inner.top(), right - left, inner.height())

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#1a1a1a"))

        # Solid block first, hatching over it: "taken", while still showing how
        # much of the universe is gone.
        hatch = QBrush(QColor("#242424"), Qt.BrushStyle.BDiagPattern)
        for first, last in self._used:
            rect = self._span_rect(first, last)
            painter.fillRect(rect, QColor("#565656"))
            painter.fillRect(rect, hatch)

        if self._pending:
            painter.fillRect(self._span_rect(*self._pending),
                             QColor("#e05252" if self._conflict else "#79cdf0"))

        painter.setPen(self._BORDER)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        painter.end()


@dataclass(frozen=True)
class LibraryEntry:
    """One pickable fixture type, flattened for the browser."""
    key: str                      # registry key, what a patch stores as `type`
    name: str                     # the key's last path segment
    manufacturer: str             # folder, or GENERIC_MANUFACTURER
    categories: Tuple[str, ...]
    fdef: FixtureDef
    document: Optional[ofl.OflFixture]

    @property
    def search_text(self) -> str:
        return " ".join((self.name, self.manufacturer,
                         " ".join(self.categories))).lower()


def library_entries() -> List[LibraryEntry]:
    """Every resolvable fixture type, sorted by manufacturer then name."""
    entries = []
    for key, fdef in load_fixture_defs().items():
        entries.append(LibraryEntry(
            key=key,
            name=key.rsplit("/", 1)[-1],
            manufacturer=fdef.manufacturer or GENERIC_MANUFACTURER,
            categories=fdef.categories,
            fdef=fdef,
            document=fixture_document(key),
        ))
    return sorted(entries, key=lambda e: (e.manufacturer.lower(), e.name.lower()))


class AddFixtureDialog(QDialog):
    """Pick a fixture type + mode from the library, and where to patch it."""

    def __init__(self, stage_controller, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add fixture")
        self.setObjectName("AddFixtureDialog")
        self.setModal(True)
        self.resize(1180, 800)
        self.setMinimumSize(940, 600)

        self._config = stage_controller.config
        self._entries = library_entries()
        self._by_key = {entry.key: entry for entry in self._entries}
        self._entry: Optional[LibraryEntry] = None
        self._mode = ""                       # "" -> the type's default mode
        self._group_by = GROUP_BY_MANUFACTURER
        self._channel_rows: List[Tuple[QTreeWidgetItem, int]] = []
        self._mode_buttons: List[Tuple[QRadioButton, str, QFrame, QLabel]] = []
        self._used_spans = used_dmx_spans(self._config)
        self._used_channels = used_midi_channels(self._config)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_library_panel())
        body.addWidget(self._build_detail_panel(), 1)
        layout.addLayout(body, 1)
        layout.addWidget(self._build_footer())

        # The bar is as tall as the buttons beside it, and only the stylesheet
        # knows how tall that is — so ask once the dialog is assembled and its
        # `QDialog#AddFixtureDialog …` rules can match.
        self.ok_button.ensurePolished()
        self.patch_bar.setFixedHeight(self.ok_button.sizeHint().height())

        self._rebuild_tree()
        self.dmx_spin.setValue(suggest_free_dmx_start(self._config))
        self.channel_spin.setValue(self._config.next_free_channel())
        self._select_first()

    # ------------------------------------------------------------------
    # Library panel
    # ------------------------------------------------------------------
    def _build_library_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("AddFxLibrary")
        panel.setFixedWidth(332)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        search_box = QFrame()
        search_box.setObjectName("AddFxSearchBox")
        search_layout = QHBoxLayout(search_box)
        search_layout.setContentsMargins(8, 0, 8, 0)
        search_layout.setSpacing(6)
        glyph = QLabel("⌕")
        glyph.setObjectName("AddFxSearchGlyph")
        search_layout.addWidget(glyph)
        self.search_input = QLineEdit()
        self.search_input.setObjectName("AddFxSearchInput")
        self.search_input.setPlaceholderText("Search fixtures, manufacturers…")
        self.search_input.textChanged.connect(lambda _text: self._rebuild_tree())
        search_layout.addWidget(self.search_input, 1)
        self.count_label = QLabel()
        self.count_label.setObjectName("AddFxSearchCount")
        search_layout.addWidget(self.count_label)
        layout.addWidget(search_box)

        segment = QFrame()
        segment.setObjectName("AddFxSegment")
        segment_layout = QHBoxLayout(segment)
        segment_layout.setContentsMargins(2, 2, 2, 2)
        segment_layout.setSpacing(2)
        self._segment_group = QButtonGroup(self)
        self._segment_group.setExclusive(True)
        for label, mode in (("Manufacturer", GROUP_BY_MANUFACTURER),
                            ("Category", GROUP_BY_CATEGORY)):
            button = QPushButton(label)
            button.setObjectName("AddFxSegmentButton")
            button.setProperty("groupBy", mode)
            button.setCheckable(True)
            button.setChecked(mode == self._group_by)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked, m=mode: self._set_group_by(m))
            self._segment_group.addButton(button)
            segment_layout.addWidget(button)
        layout.addWidget(segment)

        self.tree = QTreeWidget()
        self.tree.setObjectName("AddFxTree")
        self.tree.setColumnCount(2)
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(14)
        self.tree.setRootIsDecorated(True)
        self.tree.header().setStretchLastSection(False)
        self.tree.setColumnWidth(0, 200)
        self.tree.setItemDelegateForColumn(1, _TreeAsideDelegate(self.tree))
        self.tree.currentItemChanged.connect(self._on_tree_selection)
        layout.addWidget(self.tree, 1)
        return panel

    def _set_group_by(self, mode: str) -> None:
        if mode == self._group_by:
            return
        self._group_by = mode
        for button in self._segment_group.buttons():
            button.setChecked(button.property("groupBy") == mode)
        self._rebuild_tree()

    def _visible_entries(self) -> List[LibraryEntry]:
        tokens = self.search_input.text().lower().split()
        if not tokens:
            return list(self._entries)
        return [entry for entry in self._entries
                if all(token in entry.search_text for token in tokens)]

    def _rebuild_tree(self) -> None:
        """Regroup + refilter, keeping the current fixture selected if it survives."""
        wanted = self._entry.key if self._entry else ""
        entries = self._visible_entries()
        # Groups start collapsed; a search only makes sense with its matches shown.
        searching = bool(self.search_input.text().split())
        self.count_label.setText(f"{len(entries)} / {len(self._entries)}")

        groups: Dict[str, List[LibraryEntry]] = {}
        for entry in entries:
            if self._group_by == GROUP_BY_MANUFACTURER:
                groups.setdefault(entry.manufacturer, []).append(entry)
            else:
                # A fixture legitimately belongs to several categories, so it
                # appears under each one it declares.
                for category in entry.categories or ("Uncategorised",):
                    groups.setdefault(category, []).append(entry)

        self.tree.blockSignals(True)
        self.tree.clear()
        restored = None
        for group in sorted(groups, key=str.lower):
            members = groups[group]
            # Column 1 — the count badge and the leaves' captions — is painted
            # by `_TreeAsideDelegate`, so it needs no colour or alignment here.
            parent = QTreeWidgetItem(self.tree, [group, str(len(members))])
            parent.setFlags(Qt.ItemFlag.ItemIsEnabled)
            for entry in members:
                secondary = (entry.categories[0] if entry.categories else "")
                if self._group_by == GROUP_BY_CATEGORY:
                    secondary = entry.manufacturer
                child = QTreeWidgetItem(parent, [f"  {entry.name}", secondary])
                child.setData(0, Qt.ItemDataRole.UserRole, entry.key)
                if entry.key == wanted and restored is None:
                    restored = child
            parent.setExpanded(searching)
        self.tree.blockSignals(False)

        # A selection filtered out of the tree stays shown in the detail panel
        # rather than blanking what the user was reading.
        if restored is not None:
            self.tree.setCurrentItem(restored)

    def _select_first(self) -> None:
        iterator = self.tree.topLevelItem(0)
        if iterator is not None and iterator.childCount():
            self.tree.setCurrentItem(iterator.child(0))

    def _on_tree_selection(self, current, _previous) -> None:
        if current is None:
            return
        key = current.data(0, Qt.ItemDataRole.UserRole)
        if key:
            self.select_type(key)

    # ------------------------------------------------------------------
    # Detail panel
    # ------------------------------------------------------------------
    def _build_detail_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("AddFxDetail")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(20, 16, 20, 12)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(16)
        identity = QVBoxLayout()
        identity.setSpacing(4)
        self.detail_manufacturer = QLabel()
        self.detail_manufacturer.setObjectName("AddFxDetailManufacturer")
        identity.addWidget(self.detail_manufacturer)
        self.detail_name = QLabel()
        self.detail_name.setObjectName("AddFxDetailName")
        identity.addWidget(self.detail_name)
        self.category_chips = QWidget()
        self._category_layout = FlowLayout(self.category_chips)
        identity.addWidget(self.category_chips)
        header.addLayout(identity, 1)

        self.stat_channels = self._add_stat(header, "CHANNELS")
        self.stat_modes = self._add_stat(header, "MODES")
        self.stat_power = self._add_stat(header, "POWER")
        layout.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("AddFxTabs")
        self.tabs.addTab(self._build_general_tab(), "General")
        self.tabs.addTab(self._build_channels_tab(), "Channels")
        self.tabs.addTab(self._build_modes_tab(), "Modes")
        # Edits the fixture *library* (its sidecar scene banks), not the patch
        # this dialog is building — so it is unaffected by OK/Cancel.
        self.scenes_tab = FixtureScenesTab()
        self.tabs.addTab(self.scenes_tab, "Scenes")
        layout.addWidget(self.tabs, 1)
        return panel

    def _add_stat(self, layout: QHBoxLayout, title: str) -> QLabel:
        block = QVBoxLayout()
        block.setSpacing(2)
        caption = QLabel(title)
        caption.setObjectName("AddFxStatLabel")
        caption.setAlignment(Qt.AlignmentFlag.AlignRight)
        block.addWidget(caption)
        value = QLabel("—")
        value.setObjectName("AddFxStatValue")
        value.setAlignment(Qt.AlignmentFlag.AlignRight)
        block.addWidget(value)
        block.addStretch(1)
        layout.addLayout(block)
        return value

    def _build_general_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setObjectName("AddFxScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        page = QWidget()
        columns = QHBoxLayout(page)
        columns.setContentsMargins(16, 16, 16, 16)
        columns.setSpacing(16)

        left = QVBoxLayout()
        left.setSpacing(12)
        self.physical_card, self._physical_body = self._build_card("PHYSICAL")
        left.addWidget(self.physical_card)
        left.addStretch(1)

        right = QVBoxLayout()
        right.setSpacing(12)
        self.metadata_card, self._metadata_body = self._build_card("METADATA")
        right.addWidget(self.metadata_card)
        self.links_card, self._links_body = self._build_card("LINKS")
        right.addWidget(self.links_card)
        self.comment_card, self._comment_body = self._build_card("COMMENT")
        right.addWidget(self.comment_card)
        right.addStretch(1)

        columns.addLayout(left, 1)
        columns.addLayout(right, 1)
        scroll.setWidget(page)
        return scroll

    def _build_card(self, title: str) -> Tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("AddFxCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        header = QLabel(title)
        header.setObjectName("AddFxCardTitle")
        layout.addWidget(header)
        body_holder = QWidget()
        body = QVBoxLayout(body_holder)
        body.setContentsMargins(14, 10, 14, 12)
        body.setSpacing(7)
        layout.addWidget(body_holder)
        return card, body

    def _build_channels_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        caption = QHBoxLayout()
        self.channels_caption = QLabel()
        self.channels_caption.setObjectName("AddFxHint")
        caption.addWidget(self.channels_caption)
        caption.addStretch(1)
        self.channels_range = QLabel()
        self.channels_range.setObjectName("AddFxHint")
        caption.addWidget(self.channels_range)
        layout.addLayout(caption)

        self.channel_table = QTreeWidget()
        self.channel_table.setObjectName("AddFxChannels")
        self.channel_table.setColumnCount(4)
        self.channel_table.setHeaderLabels(["ADDRESS", "CHANNEL", "TYPE", ""])
        self.channel_table.setRootIsDecorated(False)
        self.channel_table.setIndentation(0)
        self.channel_table.setColumnWidth(0, 110)
        self.channel_table.setColumnWidth(2, 150)
        self.channel_table.setColumnWidth(3, 22)
        self.channel_table.header().setStretchLastSection(False)
        self.channel_table.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        self.channel_table.itemClicked.connect(self._toggle_channel_row)
        self.channel_table.itemExpanded.connect(
            lambda item: item.setText(3, "▾"))
        self.channel_table.itemCollapsed.connect(
            lambda item: item.setText(3, "▸" if item.childCount() else ""))
        layout.addWidget(self.channel_table, 1)
        return page

    def _toggle_channel_row(self, item: QTreeWidgetItem, _column: int) -> None:
        if item.parent() is None and item.childCount():
            item.setExpanded(not item.isExpanded())

    def _build_modes_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setObjectName("AddFxScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        page = QWidget()
        self._modes_layout = QVBoxLayout(page)
        self._modes_layout.setContentsMargins(16, 16, 16, 16)
        self._modes_layout.setSpacing(10)
        self._modes_layout.addStretch(1)
        self._mode_group = QButtonGroup(self)
        scroll.setWidget(page)
        return scroll

    # ------------------------------------------------------------------
    # Footer
    # ------------------------------------------------------------------
    def _build_footer(self) -> QWidget:
        footer = QFrame()
        footer.setObjectName("AddFxFooter")
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(16, 10, 16, 12)
        layout.setSpacing(24)

        # DMX start + what it would occupy.
        dmx_block = QVBoxLayout()
        dmx_block.setSpacing(4)
        dmx_block.addWidget(self._footer_label("DMX START"))
        dmx_row = QHBoxLayout()
        dmx_row.setSpacing(10)
        self.dmx_spin = QSpinBox()
        self.dmx_spin.setObjectName("AddFxDmxSpin")
        self.dmx_spin.setRange(1, UNIVERSE_SIZE)
        self.dmx_spin.setFixedWidth(96)
        self.dmx_spin.valueChanged.connect(lambda _value: self._on_patch_changed())
        dmx_row.addWidget(self.dmx_spin)
        occupancy = QVBoxLayout()
        occupancy.setSpacing(1)
        self.occupies_label = QLabel()
        self.occupies_label.setObjectName("AddFxOccupies")
        occupancy.addWidget(self.occupies_label)
        self.dmx_status_label = QLabel()
        self.dmx_status_label.setObjectName("AddFxStatus")
        occupancy.addWidget(self.dmx_status_label)
        dmx_row.addLayout(occupancy)
        dmx_block.addLayout(dmx_row)
        layout.addLayout(dmx_block)

        # MIDI channel: a spinbox and the same 16 channels as buttons.
        channel_block = QVBoxLayout()
        channel_block.setSpacing(4)
        channel_block.addWidget(self._footer_label("MIDI CHANNEL"))
        channel_row = QHBoxLayout()
        channel_row.setSpacing(10)
        self.channel_spin = QSpinBox()
        self.channel_spin.setObjectName("AddFxChannelSpin")
        # 0-15, the raw MIDI channel — what the patch stores and what every
        # other surface (timeline lanes, inspector) shows.
        self.channel_spin.setRange(0, 15)
        self.channel_spin.setFixedWidth(78)
        self.channel_spin.valueChanged.connect(lambda _value: self._on_patch_changed())
        channel_row.addWidget(self.channel_spin)
        buttons = QHBoxLayout()
        buttons.setSpacing(2)
        self._channel_buttons: List[QPushButton] = []
        for channel in range(16):
            button = QPushButton(str(channel))
            button.setObjectName("AddFxChannelButton")
            button.setCheckable(True)
            button.setFixedSize(21, 24)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(
                lambda _checked, c=channel: self.channel_spin.setValue(c))
            self._channel_buttons.append(button)
            buttons.addWidget(button)
        channel_row.addLayout(buttons)
        self.channel_status_label = QLabel()
        self.channel_status_label.setObjectName("AddFxStatus")
        channel_row.addWidget(self.channel_status_label)
        channel_block.addLayout(channel_row)
        layout.addLayout(channel_block)

        # The universe read-out sits centred in the gap between the MIDI
        # channels and the actions — hence a stretch on either side of it — and
        # hangs from the bottom of the row, which is what puts its bar on the
        # same line as the buttons.
        layout.addStretch(1)

        patch_block = QVBoxLayout()
        patch_block.setSpacing(4)
        patch_block.addStretch(1)
        self.patch_summary = QLabel()
        self.patch_summary.setObjectName("AddFxFieldLabel")
        patch_block.addWidget(self.patch_summary)
        # No stretch anywhere in this column, so it is exactly as wide as the
        # label and the bar takes that width.
        self.patch_bar = PatchBar()
        patch_block.addWidget(self.patch_bar)
        layout.addLayout(patch_block)

        layout.addStretch(1)

        self.ok_button = QPushButton("✓  OK")
        self.ok_button.setObjectName("AddFxOk")
        self.ok_button.setDefault(True)
        self.ok_button.clicked.connect(self.accept)
        cancel = QPushButton("⊘  Cancel")
        cancel.setObjectName("AddFxCancel")
        cancel.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(self.ok_button)
        buttons.addWidget(cancel)
        actions = QVBoxLayout()
        actions.addStretch(1)
        actions.addLayout(buttons)
        layout.addLayout(actions)
        return footer

    def _footer_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("AddFxFieldLabel")
        return label

    # ------------------------------------------------------------------
    # Population
    # ------------------------------------------------------------------
    def select_type(self, key: str) -> None:
        """Show `key` and patch it at its default mode (no-op if unknown)."""
        entry = self._by_key.get(key)
        if entry is None or entry is self._entry:
            return
        self._entry = entry
        self._mode = ""
        self._sync_tree_selection(key)
        self._populate_detail()
        self._on_patch_changed()

    def _sync_tree_selection(self, key: str) -> None:
        """Point the tree cursor at `key` — so selecting a type in code (or from
        a group the user collapsed) leaves the two halves agreeing."""
        for index in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(index)
            for child_index in range(group.childCount()):
                child = group.child(child_index)
                if child.data(0, Qt.ItemDataRole.UserRole) != key:
                    continue
                if child is not self.tree.currentItem():
                    self.tree.blockSignals(True)
                    self.tree.setCurrentItem(child)
                    self.tree.blockSignals(False)
                return

    def set_mode(self, mode: str) -> None:
        """Resolve the shown fixture to `mode` (its default when empty)."""
        if mode == self._mode:
            return
        self._mode = mode
        for button, name, card, _label in self._mode_buttons:
            button.setChecked(name == mode)
            card.setProperty("selected", "true" if name == mode else "false")
            card.style().unpolish(card)
            card.style().polish(card)
        self._populate_channels()
        self._populate_stats()
        self._populate_scenes()
        self._on_patch_changed()

    def _current_def(self) -> Optional[FixtureDef]:
        if self._entry is None:
            return None
        return resolve_fixture(self._entry.key, self._mode) or self._entry.fdef

    def _populate_detail(self) -> None:
        entry = self._entry
        if entry is None:
            return
        self.detail_manufacturer.setText(entry.manufacturer.upper())
        self.detail_name.setText(entry.name)

        _clear_layout(self._category_layout)
        for category in entry.categories:
            self._category_layout.addWidget(_chip(category, _CATEGORY_COLOR))

        document = entry.document
        _fill_rows(self._physical_body,
                   _physical_rows(document.physical if document else entry.fdef.physical))
        self.physical_card.setVisible(self._physical_body.count() > 0)

        _fill_rows(self._metadata_body, _metadata_rows(document) if document else [])
        self.metadata_card.setVisible(self._metadata_body.count() > 0)

        _clear_layout(self._links_body)
        links = document.links if document else {}
        for kind, label in (("manual", "User manual (PDF)"),
                            ("productPage", "Product page"),
                            ("video", "Video"), ("other", "More")):
            for url in links.get(kind, ()):
                button = QPushButton(f"◆  {label}")
                button.setObjectName("AddFxLinkButton")
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.setToolTip(url)
                button.clicked.connect(
                    lambda _checked, u=url: QDesktopServices.openUrl(QUrl(u)))
                self._links_body.addWidget(button)
        self.links_card.setVisible(self._links_body.count() > 0)

        _clear_layout(self._comment_body)
        comment = document.comment if document else ""
        if comment:
            text = QLabel(comment)
            text.setObjectName("AddFxComment")
            text.setWordWrap(True)
            self._comment_body.addWidget(text)
        self.comment_card.setVisible(bool(comment))

        self._populate_modes()
        self._populate_channels()
        self._populate_stats()
        self._populate_scenes()

    def _populate_scenes(self) -> None:
        """Point the Scenes tab at the type + mode now on screen. The channel
        set — and so every scene value — depends on the mode, so this follows
        both."""
        self.scenes_tab.set_fixture(self._entry.key if self._entry else "",
                                    self._current_def(), self.dmx_spin.value())

    def _populate_stats(self) -> None:
        fdef = self._current_def()
        document = self._entry.document if self._entry else None
        self.stat_channels.setText(str(fdef.channel_count) if fdef else "—")
        self.stat_modes.setText(str(len(document.modes)) if document else "1")
        power = fdef.physical.power_w if fdef else None
        self.stat_power.setText(f"{_number(power)} W" if power else "—")

    def _populate_modes(self) -> None:
        for button, _name, card, _label in self._mode_buttons:
            self._mode_group.removeButton(button)
            card.setParent(None)
        self._mode_buttons = []
        document = self._entry.document if self._entry else None
        if document is None:
            return

        for mode in document.modes:
            card = QFrame()
            card.setObjectName("AddFxModeCard")
            card.setProperty("selected", "false")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(14, 12, 14, 12)
            card_layout.setSpacing(8)

            head = QHBoxLayout()
            head.setSpacing(8)
            button = QRadioButton()
            button.setObjectName("AddFxModeRadio")
            button.clicked.connect(lambda _checked, m=mode.name: self.set_mode(m))
            self._mode_group.addButton(button)
            head.addWidget(button)
            title = QLabel(mode.name)
            title.setObjectName("AddFxModeName")
            head.addWidget(title)
            if mode.short_name and mode.short_name != mode.name:
                head.addWidget(_chip(mode.short_name, "#8f8f8f"))
            head.addStretch(1)
            footprint = QLabel()
            footprint.setObjectName("AddFxHint")
            head.addWidget(footprint)
            card_layout.addLayout(head)

            chips = QWidget()
            chip_layout = FlowLayout(chips)
            for slot in mode.channels:
                chip_layout.addWidget(_chip(
                    slot.channel.name if slot.channel else "—", "#9a9a9a"))
            card_layout.addWidget(chips)

            self._modes_layout.insertWidget(self._modes_layout.count() - 1, card)
            self._mode_buttons.append((button, mode.name, card, footprint))

        # A single-mode type keeps `self._mode` empty, which stores as "the
        # type's default" and so survives that mode being renamed later. With a
        # choice to make, the default mode becomes an explicit selection.
        default = self._current_def()
        if len(self._mode_buttons) > 1 and default is not None:
            self._mode = default.mode
        for button, name, card, _label in self._mode_buttons:
            chosen = bool(default and name == default.mode)
            button.setChecked(chosen)
            card.setProperty("selected", "true" if chosen else "false")

    def _populate_channels(self) -> None:
        self.channel_table.clear()
        self._channel_rows = []
        fdef = self._current_def()
        if fdef is None:
            return
        self.channels_caption.setText(
            f"Layout for mode <b>{fdef.mode}</b> — addresses shown from DMX start")
        for index, spec in enumerate(fdef.channels):
            label, color = _channel_family(spec)
            # A fine slot carries its coarse channel's label, so name it by its
            # own key ("Pan fine") — the row is a distinct DMX address.
            name = spec.field if spec.fine_of >= 0 else (spec.label or spec.field)
            item = QTreeWidgetItem(self.channel_table, ["", name, "", ""])
            item.setForeground(0, QColor("#8f8f8f"))
            self.channel_table.setItemWidget(item, 2, _chip_cell(label, color))
            for cap in spec.capabilities:
                low, high = cap.dmx_range
                child = QTreeWidgetItem(
                    item, [f"{low:>3} – {high:<3}", _capability_summary(cap),
                           cap.type, ""])
                child.setForeground(0, QColor("#6f6f6f"))
                child.setForeground(1, QColor("#b4b4b4"))
                child.setForeground(2, QColor("#6f6f6f"))
            item.setText(3, "▸" if item.childCount() else "")
            item.setForeground(3, QColor("#6f6f6f"))
            self._channel_rows.append((item, index))
        self._refresh_addresses()

    def _refresh_addresses(self) -> None:
        """Re-stamp the ADDRESS column against the current DMX start."""
        start = self.dmx_spin.value()
        for item, index in self._channel_rows:
            item.setText(0, f"{index + 1:02d} · {start + index}")
        self.scenes_tab.set_dmx_start(start)
        fdef = self._current_def()
        if fdef and fdef.channel_count:
            self.channels_range.setText(
                f"DMX {start} – {start + fdef.channel_count - 1}")
        else:
            self.channels_range.setText("")
        for _button, name, _card, label in self._mode_buttons:
            resolved = resolve_fixture(self._entry.key, name) if self._entry else None
            count = resolved.channel_count if resolved else 0
            label.setText(f"{count} ch · {start}-{start + max(0, count - 1)}")

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def _on_patch_changed(self) -> None:
        fdef = self._current_def()
        count = fdef.channel_count if fdef else 0
        start = self.dmx_spin.value()
        last = start + max(0, count - 1)
        channel = self.channel_spin.value()

        self._refresh_addresses()
        self.occupies_label.setText(
            f"occupies {start} – {last}" if count else "occupies nothing")

        overflow = count and last > UNIVERSE_SIZE
        clash = next((name for name, first, end in self._used_spans
                      if count and start <= end and last >= first), "")
        if overflow:
            state, message = "error", f"needs {count} ch · past {UNIVERSE_SIZE}"
        elif clash:
            state, message = "error", f"overlaps {clash}"
        else:
            state, message = "ok", "free · no overlap"
        self.dmx_status_label.setText(message)
        _set_state(self.dmx_status_label, state)

        channel_taken = channel in self._used_channels
        for index, button in enumerate(self._channel_buttons):
            button.setChecked(index == channel)
            button.setProperty("used", "true" if index in self._used_channels
                               else "false")
            button.style().unpolish(button)
            button.style().polish(button)
        # The channel is a fixture's identity for every saved clip, so a
        # duplicate is refused rather than silently reassigned.
        self.channel_status_label.setText("already used" if channel_taken else "")
        # Empty, it still costs the row its spacing, which pushes the universe
        # block off the centre of the gap it is supposed to sit in.
        self.channel_status_label.setVisible(channel_taken)
        _set_state(self.channel_status_label, "error")

        used_total = sum(end - first + 1 for _n, first, end in self._used_spans)
        self.patch_summary.setText(
            f"UNIVERSE {self._config.artnet.universe} · PATCH "
            f"{used_total + count} / {UNIVERSE_SIZE} USED")
        self.patch_bar.set_spans([(first, end) for _n, first, end in self._used_spans],
                                 (start, min(last, UNIVERSE_SIZE)) if count else None,
                                 bool(overflow or clash))
        self.ok_button.setEnabled(
            bool(fdef) and not overflow and not clash and not channel_taken)

    # ------------------------------------------------------------------
    # Result
    # ------------------------------------------------------------------
    def values(self):
        """Return (type_name, dmx_start, channel, mode) for the chosen fixture.

        `mode` is empty for a single-mode type, which stores as "the type's
        default" and so survives the mode being renamed in its definition."""
        key = self._entry.key if self._entry else ""
        mode = self._mode if len(self._mode_buttons) > 1 else ""
        return (key, self.dmx_spin.value(), self.channel_spin.value(), mode)

    def done(self, result: int) -> None:
        # Scene edits are library edits, so neither button here decides their
        # fate: an unsaved one asks on the way out, whichever closed the dialog.
        self.scenes_tab.save_pending_changes()
        self.scenes_tab.preview.clear()
        super().done(result)


# --- shared construction helpers ---------------------------------------------

def _chip_cell(text: str, color: str) -> QWidget:
    """A chip in a table cell: the trailing stretch keeps it text-sized rather
    than letting the label fill the column."""
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(_chip(text, color))
    layout.addStretch(1)
    return holder


def _set_state(label: QLabel, state: str) -> None:
    label.setProperty("state", state)
    label.style().unpolish(label)
    label.style().polish(label)


def _clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)


def _fill_rows(layout: QVBoxLayout, rows: List[Tuple[str, str]]) -> None:
    """Replace a card body with `rows` of label + monospaced value."""
    _clear_layout(layout)
    for name, value in rows:
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(12)
        caption = QLabel(name)
        caption.setObjectName("AddFxRowLabel")
        caption.setFixedWidth(120)
        row_layout.addWidget(caption)
        text = QLabel(value)
        text.setObjectName("AddFxRowValue")
        text.setWordWrap(True)
        text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row_layout.addWidget(text, 1)
        layout.addWidget(row)
