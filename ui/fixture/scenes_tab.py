# ABOUTME: The fixture browser's Scenes tab — edits a type's sidecar scene banks
# ABOUTME: with a live preview drawn by the real stage render engine.
"""Scene editing for one fixture type.

A scene is a snapshot of every channel of a fixture, stored in the `.tdmx.json`
sidecar's `banks` (see `core/fixture_sidecar.py`). Banks are what MIDI addresses:
note 36+n selects the n-th bank, note 24+n the n-th scene inside it — so both
orders are semantic, and this editor keeps them visible rather than flattening
them into one list.

    SCENES               ┃  SCENE NAME  [ Open white  ] [Create]
    ▾ ANIMATIONS      8  ┃  SONG PARTS  (INTRO)(VERSE)(CHORUS)…
       ▪ Cuadrados       ┃  ┌ SIMULATED OUTPUT ──────────────┐
       ▪ Ping Pong       ┃  │ intensity 100 %      <the real │
    ▾ FILLS           8  ┃  │ colour    white       renderer>│
       ▪ Fuego           ┃  └────────────────────────────────┘
                         ┃  ┌ CHANNEL FADERS ────────────────┐
    [+ New] [Dup] [Del]  ┃  │ ▮ ▮ ▮ ▮ ▮ ▮ ▮ ▮ ▮ ▮            │

Nothing here is simulated twice: the preview builds the same renderer the stage
would (`stage.registry.renderer_for`) and feeds it the scene's DMX values, so
what a scene looks like here is what it looks like on stage — animations, beam
cone, strobe and all.

The tab opens on a blank **draft**: nothing is selected, the faders sit at their
channel defaults, and the button beside the name reads "Create" — dial a look in
and press it, and the draft becomes a scene in the target bank. Clicking a scene
in the list loads it instead, and the button becomes "Save", which overwrites
that one. Either way that button is the only thing that writes the sidecar; it
is deliberately NOT the surrounding dialog's OK, because this edits the fixture
*library*, not the patch that dialog is building. Leaving the fixture, or
closing the dialog, with work pending asks whether to write it.

One caveat this UI cannot hide: adding, deleting or reordering a scene shifts
the indices after it, and a saved .mid binds a scene by index. Renaming a bank
is safe (the chases naming it are rewritten to follow).
"""

from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QMenu, QMessageBox, QPushButton, QToolButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core.fixture_library import FixtureDef, save_fixture_banks
from core.fixture_state import FixtureState, resolve_state
from core.note_layout import SONG_PARTS
from ui.widgets.chips import flow_holder
# The console — faders, preview and read-out — is shared with the timeline's
# clip editor, so a scene looks the same wherever it is edited. Re-exported
# under their old private names for the callers (and tests) that reach in.
from ui.fixture.console import (  # noqa: F401  (re-exported)
    ChannelFader, FaderSlider as _FaderSlider, FaderStrip, OutputCard,
    ScenePreview, capability_rows as _capability_rows, clamp as _clamp,
    colour_text as _colour_text, escape as _escape, scene_values,
    strobe_text as _strobe_text, summary_html as _summary_html,
)
from tupperlogger import get_logger

logger = get_logger("ui.fixture.scenes_tab")

DEFAULT_BANK_NAME = "SCENES"

# Roles carried on the tree items, so a selection resolves back to its data
# without a parallel index.
_ROLE_BANK = Qt.ItemDataRole.UserRole
_ROLE_INDEX = Qt.ItemDataRole.UserRole + 1


# --- song-part tags -----------------------------------------------------------

class _PartChip(QLabel):
    """One song-part tag: a chip you click, not a button.

    A label rather than a checkable QPushButton because that is what these are
    — tags — and a button carries chrome a tag shouldn't: its own minimum size
    and the pressed/raised look every other button in the dialog has. The state
    rides on a `checked` property, which is how the stylesheet reaches it (a
    label has no `:checked` pseudo-state).
    """

    clicked = pyqtSignal()

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setObjectName("AddFxSongPart")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._checked = False
        self.setProperty("checked", "false")

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, checked: bool) -> None:
        if checked == self._checked:
            return
        self._checked = checked
        self.setProperty("checked", "true" if checked else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event):
        # A disabled widget never gets here, so there is nothing to guard.
        if event.button() == Qt.MouseButton.LeftButton:
            self.setChecked(not self._checked)
            self.clicked.emit()
            return
        super().mousePressEvent(event)


# --- the scene list -----------------------------------------------------------

class _SceneTree(QTreeWidget):
    """Banks and their scenes, reorderable by drag.

    The drop is resolved to a (source, destination) pair and reported; the items
    themselves are never moved by Qt. The tab owns the ordering, and rebuilding
    the tree from the data afterwards is the only way the two can't drift —
    letting Qt reshuffle the items would leave their bank/index roles pointing
    at where the scene used to be.
    """

    # bank name, scene index (-1 for the bank itself) -> bank name, position
    move_requested = pyqtSignal(str, int, str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("AddFxScenes")
        self.setColumnCount(2)
        self.setHeaderHidden(True)
        self.setIndentation(12)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(1, 46)

    def dropEvent(self, event):
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()
        dragged = self.currentItem()
        target = self.itemAt(event.position().toPoint())
        if dragged is None:
            return
        destination = self._destination(dragged, target,
                                        self.dropIndicatorPosition())
        if destination is None:
            return
        source_index = dragged.data(0, _ROLE_INDEX)
        self.move_requested.emit(dragged.data(0, _ROLE_BANK),
                                 -1 if source_index is None else source_index,
                                 destination[0], destination[1])

    def _destination(self, dragged, target,
                     position) -> Optional[Tuple[str, int]]:
        """Where the drop lands, or None when it isn't a legal shape.

        A bank stays top-level; a scene always ends up inside some bank.
        """
        above = position == QAbstractItemView.DropIndicatorPosition.AboveItem
        onto = position == QAbstractItemView.DropIndicatorPosition.OnItem
        is_bank = dragged.parent() is None

        if is_bank:
            # Banks only reorder among banks. Dropping past the last one (empty
            # space, or below a bank) appends.
            if target is None:
                return ("", self.topLevelItemCount())
            bank_item = target if target.parent() is None else target.parent()
            index = self.indexOfTopLevelItem(bank_item)
            return ("", index if above else index + 1)

        if target is None:
            return None
        if target.parent() is None:
            # Onto a bank header: the front of that bank.
            return (target.data(0, _ROLE_BANK), 0) if onto else None
        index = target.parent().indexOfChild(target)
        return (target.data(0, _ROLE_BANK), index if above else index + 1)


# --- the tab ------------------------------------------------------------------

class FixtureScenesTab(QWidget):
    """List, edit and preview the scene banks of one fixture type."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._type_name = ""
        self._fdef: Optional[FixtureDef] = None
        self._dmx_start = 1
        # bank name -> list of scene dicts. Owned outright: mutated in place,
        # and nothing reaches the sidecar until Save is pressed.
        self._banks: Dict[str, List[dict]] = {}
        self._chases: List[dict] = []
        self._selected: Optional[Tuple[str, int]] = None   # (bank, index)
        # The scene being composed while nothing is selected: it belongs to no
        # bank yet, which is exactly what Create is for.
        self._draft: Optional[dict] = None
        self._part_chips: Dict[str, _PartChip] = {}
        # Whether `self._banks` has run ahead of the sidecar. A draft never
        # counts — it isn't in them yet.
        self._dirty = False
        # A bank rename has to rewrite the chases naming it, so that one edit
        # widens what the next save writes.
        self._save_chases = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(14)
        layout.addWidget(self._build_list_panel())
        layout.addWidget(self._build_editor_panel(), 1)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_list_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("AddFxScenesPanel")
        panel.setFixedWidth(238)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(6)
        title = QLabel("SCENES")
        title.setObjectName("AddFxFieldLabel")
        header.addWidget(title)
        header.addStretch(1)
        self.new_button = QToolButton()
        self.new_button.setObjectName("AddFxNewScene")
        self.new_button.setText("+ New")
        self.new_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.new_button)
        menu.addAction("New scene", self._new_draft)
        menu.addAction("New bank…", self._add_bank)
        self.new_button.setMenu(menu)
        header.addWidget(self.new_button)
        layout.addLayout(header)

        self.tree = _SceneTree()
        self.tree.currentItemChanged.connect(self._on_tree_selection)
        self.tree.itemChanged.connect(self._on_item_renamed)
        self.tree.move_requested.connect(self._on_move)
        layout.addWidget(self.tree, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.duplicate_button = QPushButton("Duplicate")
        self.duplicate_button.setObjectName("AddFxSceneAction")
        self.duplicate_button.clicked.connect(self._duplicate)
        buttons.addWidget(self.duplicate_button)
        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("AddFxSceneAction")
        self.delete_button.clicked.connect(self._delete)
        buttons.addWidget(self.delete_button)
        layout.addLayout(buttons)

        self.status_label = QLabel()
        self.status_label.setObjectName("AddFxHint")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        return panel

    def _build_editor_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        name_row = QHBoxLayout()
        name_row.setSpacing(12)
        caption = QLabel("SCENE NAME")
        caption.setObjectName("AddFxFieldLabel")
        caption.setFixedWidth(96)
        name_row.addWidget(caption)
        self.name_input = QLineEdit()
        self.name_input.setObjectName("AddFxSceneName")
        self.name_input.setPlaceholderText("Unnamed scene")
        self.name_input.editingFinished.connect(self._on_name_edited)
        name_row.addWidget(self.name_input, 1)
        self.save_button = QPushButton("Save")
        self.save_button.setObjectName("AddFxSceneSave")
        self.save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_button.setToolTip(
            "Write this scene to the fixture's .tdmx.json sidecar")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._on_save_clicked)
        name_row.addWidget(self.save_button)
        layout.addLayout(name_row)

        parts_row = QHBoxLayout()
        parts_row.setSpacing(12)      # same gutter as the name row above
        parts_caption = QLabel("SONG PARTS")
        parts_caption.setObjectName("AddFxFieldLabel")
        parts_caption.setFixedWidth(96)
        parts_row.addWidget(parts_caption, 0, Qt.AlignmentFlag.AlignTop)
        # The nine tags fit one row at the dialog's own width; the flow layout
        # is there for when it doesn't — squeezed into a QHBoxLayout they would
        # overlap instead. Each is sized by the stylesheet's font, which is the
        # only thing that knows how wide the text really is.
        holder = flow_holder(spacing=4)
        for part in SONG_PARTS:
            chip = _PartChip(part)
            chip.setToolTip(f"Offer this scene automatically during {part}")
            chip.clicked.connect(self._on_song_parts_changed)
            self._part_chips[part] = chip
            holder.layout().addWidget(chip)
        parts_row.addWidget(holder, 1)
        layout.addLayout(parts_row)

        self._output = OutputCard()
        self._faders = FaderStrip()
        self._faders.value_changed.connect(self._on_fader_changed)
        layout.addWidget(self._output, 1)
        layout.addWidget(self._faders)
        return panel

    @property
    def preview(self):
        """The stage preview inside the output card. Kept as a direct handle
        because `AddFixtureDialog` calls `scenes_tab.preview.clear()` on close —
        the bodies own animation timers on the shared 30 Hz clock."""
        return self._output.preview

    @property
    def summary_label(self):
        """The SIMULATED OUTPUT read-out inside the output card."""
        return self._output.summary_label

    # ------------------------------------------------------------------
    # Population
    # ------------------------------------------------------------------
    def set_fixture(self, type_name: str, fdef: Optional[FixtureDef],
                    dmx_start: int) -> None:
        """Show `fdef`'s banks. Called whenever the browser's type or mode
        changes — the channel set, and so every scene, depends on the mode."""
        # Unsaved edits belong to the OUTGOING fixture; settle them before
        # `self._banks` becomes someone else's.
        self.save_pending_changes()
        self._type_name = type_name
        self._fdef = fdef
        self._dmx_start = dmx_start
        self._selected = None
        # Deep-ish copy: the scene dicts are edited in place, and fdef.banks is
        # handed out of the memoized registry.
        self._banks = {name: [dict(scene) for scene in scenes]
                       for name, scenes in (fdef.banks if fdef else {}).items()}
        self._chases = [dict(chase) for chase in (fdef.chases if fdef else [])]
        self._clear_pending()
        # Open on a blank scene rather than on somebody else's: the faders are
        # live from the first click, and Create is what puts them in a bank.
        self._draft = self._default_scene() if fdef else None

        self._faders.set_fixture(fdef, dmx_start)
        self._output.set_fixture(fdef)
        self._rebuild_tree()
        self._refresh_editor()

    def set_dmx_start(self, dmx_start: int) -> None:
        """Re-stamp the fader addresses against a new DMX start."""
        self._dmx_start = dmx_start
        self._faders.set_dmx_start(dmx_start)

    def _rebuild_tree(self) -> None:
        """Relay the whole tree from `self._banks`, keeping the selection."""
        wanted = self._selected
        self.tree.blockSignals(True)
        self.tree.clear()
        restored = None
        for bank_name, scenes in self._banks.items():
            parent = QTreeWidgetItem(self.tree, [bank_name, str(len(scenes))])
            parent.setData(0, _ROLE_BANK, bank_name)
            parent.setFlags(parent.flags() | Qt.ItemFlag.ItemIsEditable)
            parent.setForeground(1, QColor("#7a7a7a"))
            parent.setTextAlignment(1, int(Qt.AlignmentFlag.AlignRight
                                           | Qt.AlignmentFlag.AlignVCenter))
            for index, scene in enumerate(scenes):
                child = QTreeWidgetItem(parent, [self._scene_label(scene, index),
                                                 self._parts_label(scene)])
                child.setData(0, _ROLE_BANK, bank_name)
                child.setData(0, _ROLE_INDEX, index)
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsEditable)
                child.setIcon(0, self._swatch(scene))
                child.setForeground(1, QColor("#7a7a7a"))
                child.setTextAlignment(1, int(Qt.AlignmentFlag.AlignRight
                                              | Qt.AlignmentFlag.AlignVCenter))
                parts = self._parts_of(scene)
                if parts:
                    child.setToolTip(1, ", ".join(parts))
                if wanted == (bank_name, index):
                    restored = child
            parent.setExpanded(True)
        self.tree.blockSignals(False)
        if restored is not None:
            self.tree.setCurrentItem(restored)

    def _scene_label(self, scene: dict, index: int) -> str:
        return str(scene.get("name") or f"Scene {index + 1}")

    @staticmethod
    def _parts_of(scene: dict) -> List[str]:
        parts = scene.get("song_parts")
        return [str(p) for p in parts] if isinstance(parts, list) else []

    @classmethod
    def _parts_label(cls, scene: dict) -> str:
        """The song-part tags, abbreviated to fit the narrow second column —
        the full list rides along as the row's tooltip."""
        parts = cls._parts_of(scene)
        if not parts:
            return ""
        extra = f"+{len(parts) - 1}" if len(parts) > 1 else ""
        return f"{parts[0][:3]}{extra}"

    def _swatch(self, scene: dict):
        """A chip of the colour this scene resolves to, painted by the engine."""
        pixmap = QPixmap(10, 10)
        pixmap.fill(Qt.GlobalColor.transparent)
        state = self._resolve(self._scene_values(scene))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor("#4a4a4a"))
        painter.setBrush(QColor(*state.scaled_rgb()) if state else QColor("#2a2a2a"))
        painter.drawRoundedRect(0, 0, 9, 9, 2, 2)
        painter.end()
        return QIcon(pixmap)

    # ------------------------------------------------------------------
    # Selection + the editor's state
    # ------------------------------------------------------------------
    def _on_tree_selection(self, current, _previous) -> None:
        left_a_scene = self._selected is not None
        if current is None:
            self._selected = None
        else:
            bank = current.data(0, _ROLE_BANK)
            index = current.data(0, _ROLE_INDEX)
            self._selected = (bank, index) if index is not None else None
        if self._selected is None and left_a_scene:
            # Stepping off a scene starts a new one. Moving between bank headers
            # doesn't: it only re-targets the draft already being composed.
            self._draft = self._default_scene()
        self._refresh_editor()

    def _drafting(self) -> bool:
        """Whether the faders hold a scene that is in no bank yet."""
        return self._selected is None and self._draft is not None

    def _default_scene(self) -> dict:
        """A blank scene: every channel at the default its definition declares."""
        if self._fdef is None:
            return {}
        return {spec.field: _clamp(spec.default_value)
                for spec in self._fdef.channels}

    def _current_scene(self) -> Optional[dict]:
        """What the faders are editing: the selected scene, or the draft."""
        if self._selected is None:
            return self._draft
        bank, index = self._selected
        scenes = self._banks.get(bank, [])
        return scenes[index] if 0 <= index < len(scenes) else None

    def _scene_values(self, scene: Optional[dict]) -> List[int]:
        """A scene as a DMX slice, filling anything it omits with the channel's
        declared default — the same fallback `resolve_state` applies."""
        return scene_values(self._fdef, scene)

    def _resolve(self, values: List[int]) -> Optional[FixtureState]:
        if self._fdef is None or not values:
            return None
        return resolve_state(self._fdef, values)

    def _refresh_editor(self) -> None:
        """Push the scene on the faders — selected or draft — into the name,
        tags, faders and preview."""
        if self._selected is None and self._draft is None and self._fdef:
            # The invariant every path relies on: nothing selected always means
            # composing a new scene, so the faders are never dead.
            self._draft = self._default_scene()
        scene = self._current_scene()
        editable = scene is not None
        self.name_input.setEnabled(editable)
        for chip in self._part_chips.values():
            chip.setEnabled(editable)
        self._faders.set_editable(editable)
        self.duplicate_button.setEnabled(self.tree.currentItem() is not None)
        self.delete_button.setEnabled(self.tree.currentItem() is not None)
        self._refresh_save_button()

        # A draft shows an empty field with its placeholder — the "Scene N"
        # fallback is a label for something already in a bank.
        if scene is None:
            text = ""
        elif self._selected is None:
            text = str(scene.get("name", ""))
        else:
            text = self._scene_label(scene, self._selected[1])
        self.name_input.blockSignals(True)
        self.name_input.setText(text)
        self.name_input.blockSignals(False)

        parts = scene.get("song_parts") if scene else None
        parts = set(parts) if isinstance(parts, list) else set()
        for name, chip in self._part_chips.items():
            chip.setChecked(name in parts)

        values = self._scene_values(scene)
        self._faders.set_values(values)
        self._refresh_output(values)
        self._refresh_status()

    def _refresh_output(self, values: List[int]) -> None:
        self._output.set_values(values)

    def _refresh_save_button(self) -> None:
        """Create a draft, Save the banks. A draft is always there to be
        created, but a pending change to the banks themselves — a delete, a
        reorder, an edited scene — takes the button first: it is the only way
        to write one, and it leaves the draft on the faders for the press after.
        """
        offer_create = self._drafting() and not self._dirty
        self.save_button.setText("Create" if offer_create else "Save")
        self.save_button.setEnabled(offer_create or self._dirty)

    def _refresh_status(self) -> None:
        """The line under the list: what is pending, where a new scene would
        land, and why an edit might be ignored."""
        if self._fdef is None:
            self.status_label.setText("")
            return
        scene = self._current_scene()
        if (not self._drafting() and scene is not None
                and not any(self._scene_values(scene))):
            self.status_label.setText(
                "Every channel is 0 — the engine skips all-zero scenes.")
            return
        if self._dirty:
            self.status_label.setText(
                "Unsaved changes — press Save to write them to the fixture's "
                ".tdmx.json.")
            return
        if self._drafting():
            self.status_label.setText(
                f"New scene — Create adds it to {self._target_bank()}.")
            return
        total = sum(len(scenes) for scenes in self._banks.values())
        if not total:
            # A fixture taken straight from the OFL project has no banks at all.
            self.status_label.setText(
                "No scenes yet — dial one in and press Create, saved to the "
                "fixture's .tdmx.json sidecar.")
            return
        self.status_label.setText(
            f"{total} scene(s) in {len(self._banks)} bank(s) · "
            f"saved to the fixture's .tdmx.json")

    # ------------------------------------------------------------------
    # Edits
    # ------------------------------------------------------------------
    def _on_fader_changed(self, index: int, value: int) -> None:
        """Live while dragging: the scene and the preview follow the fader, and
        the sidecar catches up when Create/Save is pressed."""
        scene = self._current_scene()
        if scene is None or self._fdef is None or index >= self._fdef.channel_count:
            return
        scene[self._fdef.channels[index].field] = value
        self._refresh_output(self._scene_values(scene))
        if not self._drafting():
            self._mark_dirty()

    def _on_name_edited(self) -> None:
        scene = self._current_scene()
        if scene is None:
            return
        name = self.name_input.text().strip()
        if name == str(scene.get("name", "")):
            return
        if name:
            scene["name"] = name
        else:
            scene.pop("name", None)
        if self._drafting():
            return          # nothing in the tree to relabel yet
        self._mark_dirty()
        self._rebuild_tree()

    def _on_song_parts_changed(self) -> None:
        scene = self._current_scene()
        if scene is None:
            return
        parts = [name for name in SONG_PARTS if self._part_chips[name].isChecked()]
        if parts:
            scene["song_parts"] = parts
        else:
            scene.pop("song_parts", None)
        if self._drafting():
            return
        self._mark_dirty()
        self._rebuild_tree()

    def _on_item_renamed(self, item: QTreeWidgetItem, column: int) -> None:
        """An inline edit in the tree renames a bank or a scene."""
        if column != 0 or self._fdef is None:
            return
        text = item.text(0).strip()
        index = item.data(0, _ROLE_INDEX)
        if index is None:
            # A bank rename relays the whole tree, which would delete `item`
            # while this signal is still on the stack; run it after we return.
            old = item.data(0, _ROLE_BANK)
            QTimer.singleShot(0, lambda: self._rename_bank(old, text))
        else:
            bank = self._banks.get(item.data(0, _ROLE_BANK), [])
            if 0 <= index < len(bank):
                bank[index]["name"] = text or f"Scene {index + 1}"
                self._mark_dirty()
                self._refresh_editor()

    def _rename_bank(self, old: str, new: str) -> None:
        """Rename a bank, following through into the chases that name it.

        Chases reference a bank by NAME, so this is the one rename that can be
        made safe — and has to be, or a rename silently breaks every chase.
        """
        if not new or new == old or old not in self._banks:
            self._rebuild_tree()
            return
        if new in self._banks:
            QMessageBox.warning(self, "Rename bank",
                                f"A bank called '{new}' already exists.")
            self._rebuild_tree()
            return

        self._banks = {(new if name == old else name): scenes
                       for name, scenes in self._banks.items()}
        for chase in self._chases:
            names = chase.get("banks")
            if isinstance(names, list):
                chase["banks"] = [new if n == old else n for n in names]
        if self._selected and self._selected[0] == old:
            self._selected = (new, self._selected[1])
        self._save_chases = True
        self._mark_dirty()
        self._rebuild_tree()

    def _add_bank(self) -> None:
        if self._fdef is None:
            return
        name, ok = QInputDialog.getText(self, "New bank", "Bank name:")
        name = name.strip()
        if not ok or not name:
            return
        if name in self._banks:
            QMessageBox.warning(self, "New bank",
                                f"A bank called '{name}' already exists.")
            return
        self._banks[name] = []
        self._selected = None
        self._mark_dirty()
        self._rebuild_tree()
        self._refresh_editor()

    def _target_bank(self) -> str:
        """Which bank a created scene joins: the selected one, else the first,
        else a default one (a fixture straight from OFL has no banks at all)."""
        item = self.tree.currentItem()
        if item is not None:
            return item.data(0, _ROLE_BANK)
        return next(iter(self._banks), DEFAULT_BANK_NAME)

    def _new_draft(self) -> None:
        """Start a blank scene: the faders drop back to their defaults and the
        button offers to Create what gets dialled in."""
        if self._fdef is None:
            return
        self.tree.setCurrentItem(None)     # -> _on_tree_selection, if anything
        self._selected = None              #    was selected; force it either way
        self._draft = self._default_scene()
        self._refresh_editor()

    def _create(self) -> None:
        """Turn the draft on the faders into a real scene in its target bank."""
        if self._fdef is None or self._draft is None:
            return
        bank = self._target_bank()
        scenes = self._banks.setdefault(bank, [])
        scene = dict(self._draft)
        scene.setdefault("name", f"Scene {len(scenes) + 1}")
        scenes.append(scene)
        self._selected = (bank, len(scenes) - 1)
        self._draft = None
        self._dirty = True
        self._save()
        self._rebuild_tree()
        self._refresh_editor()

    def _duplicate(self) -> None:
        item = self.tree.currentItem()
        if item is None:
            return
        bank = item.data(0, _ROLE_BANK)
        index = item.data(0, _ROLE_INDEX)
        if index is None:
            self._duplicate_bank(bank)
            return
        scenes = self._banks.get(bank, [])
        if not 0 <= index < len(scenes):
            return
        copy = dict(scenes[index])
        copy["name"] = f"{self._scene_label(copy, index)} copy"
        scenes.insert(index + 1, copy)
        self._selected = (bank, index + 1)
        self._mark_dirty()
        self._rebuild_tree()
        self._refresh_editor()

    def _duplicate_bank(self, bank: str) -> None:
        scenes = self._banks.get(bank)
        if scenes is None:
            return
        name = f"{bank} copy"
        suffix = 2
        while name in self._banks:
            name, suffix = f"{bank} copy {suffix}", suffix + 1
        self._banks[name] = [dict(scene) for scene in scenes]
        self._selected = None
        self._mark_dirty()
        self._rebuild_tree()
        self._refresh_editor()

    def _delete(self) -> None:
        item = self.tree.currentItem()
        if item is None:
            return
        bank = item.data(0, _ROLE_BANK)
        index = item.data(0, _ROLE_INDEX)
        if index is None:
            self._delete_bank(bank)
            return
        scenes = self._banks.get(bank, [])
        if not 0 <= index < len(scenes):
            return
        del scenes[index]
        self._selected = (bank, min(index, len(scenes) - 1)) if scenes else None
        self._mark_dirty()
        self._rebuild_tree()
        self._refresh_editor()

    def _delete_bank(self, bank: str) -> None:
        scenes = self._banks.get(bank)
        if scenes is None:
            return
        # Chases name a bank, so deleting one leaves them pointing at nothing.
        affected = sum(1 for chase in self._chases
                       if bank in (chase.get("banks") or []))
        message = f"Delete the bank '{bank}' and its {len(scenes)} scene(s)?"
        if affected:
            message += f"\n\n{affected} chase(s) reference this bank."
        if QMessageBox.question(self, "Delete bank", message) != \
                QMessageBox.StandardButton.Yes:
            return
        del self._banks[bank]
        self._selected = None
        self._mark_dirty()
        self._rebuild_tree()
        self._refresh_editor()

    def _on_move(self, source_bank: str, source_index: int,
                 target_bank: str, position: int) -> None:
        """Apply a drag: move one scene, or one whole bank, to `position`.

        Both orders are what MIDI addresses (note 24+n picks the n-th scene,
        36+n the n-th bank), so a drag here re-binds notes — the same as adding
        or deleting one does.
        """
        if source_index < 0:
            self._move_bank(source_bank, position)
        else:
            self._move_scene(source_bank, source_index, target_bank, position)
        self._mark_dirty()
        self._rebuild_tree()
        self._refresh_editor()

    def _move_bank(self, name: str, position: int) -> None:
        order = [bank for bank in self._banks if bank != name]
        if name not in self._banks:
            return
        # `position` counts the dragged bank itself, so drop it from the index
        # once the list no longer contains it.
        current = list(self._banks).index(name)
        insert_at = max(0, min(len(order), position - (1 if position > current else 0)))
        order.insert(insert_at, name)
        self._banks = {bank: self._banks[bank] for bank in order}
        self._selected = None

    def _move_scene(self, source_bank: str, source_index: int,
                    target_bank: str, position: int) -> None:
        source = self._banks.get(source_bank)
        target = self._banks.get(target_bank)
        if source is None or target is None or not 0 <= source_index < len(source):
            return
        scene = source.pop(source_index)
        if source_bank == target_bank and source_index < position:
            position -= 1
        position = max(0, min(len(target), position))
        target.insert(position, scene)
        self._selected = (target_bank, position)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _on_save_clicked(self) -> None:
        """The one button: Create the draft, or Save what the banks are owed."""
        if self._drafting() and not self._dirty:
            self._create()
        else:
            self._save()

    def _draft_touched(self) -> bool:
        """A draft counts as work only once it has moved off the defaults —
        otherwise merely visiting the tab would ask on the way out."""
        return self._drafting() and self._draft != self._default_scene()

    def save_pending_changes(self) -> None:
        """Settle unsaved work on the way out — to another fixture, or out of
        the dialog. Without an autosave, leaving quietly would drop it."""
        drafted = self._draft_touched()
        if self._fdef is None or not (self._dirty or drafted):
            return
        answer = QMessageBox.question(
            self, "Unsaved scenes",
            f"Save the scene changes to '{self._type_name}'?")
        if answer != QMessageBox.StandardButton.Yes:
            self._clear_pending()
        elif drafted:
            # Writes the banks wholesale, so this covers a dirty one too.
            self._create()
        else:
            self._save()

    def _mark_dirty(self) -> None:
        """Record that the in-memory banks have run ahead of the sidecar."""
        if self._dirty:
            return
        self._dirty = True
        self._refresh_save_button()
        self._refresh_status()

    def _clear_pending(self) -> None:
        self._dirty = False
        self._save_chases = False
        self._refresh_save_button()

    def _save(self) -> None:
        """Write the banks through to the type's sidecar.

        The one thing here that touches disk. It edits the fixture library, not
        the patch the surrounding dialog builds, so the dialog's OK/Cancel has
        nothing to do with it.
        """
        if self._fdef is None or not self._type_name:
            return
        try:
            path = save_fixture_banks(
                self._type_name, self._fdef.mode, self._banks,
                self._chases if self._save_chases else None)
        except OSError as error:
            logger.error(f"Could not save scenes for '{self._type_name}': {error}")
            self.status_label.setText(f"Could not save: {error}")
            return
        if path is None:
            self.status_label.setText("Could not save — see the log.")
            return
        self._clear_pending()
        self._refresh_status()


