# ABOUTME: Clip library panel — draggable MIDI-note / scene / chase clips,
# ABOUTME: grouped data-driven from the patched fixtures' library definitions.

import json
from collections import defaultdict
from typing import Dict, List, Tuple

from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QToolBox, QListWidget,
                             QListWidgetItem, QLabel, QAbstractItemView)
from PyQt6.QtGui import QColor, QPixmap, QIcon, QPainter
from PyQt6.QtCore import Qt, QMimeData, QSize, pyqtSignal

import note_layout
from config import Config
from fixture_library import (FixtureDef, load_fixture_defs,
                             ROLE_RED, ROLE_GREEN, ROLE_BLUE)
from software_controller.scene_loader import Scene, load_scenes
from tupperlogger import get_logger


# Custom MIME type carries a JSON payload {"note": int, "velocity": int}.
# Plain-text fallback is also set so the payload is visible in tools that
# only inspect text/plain (handy for debugging the drop chain).
CLIP_MIME_TYPE = "application/x-tupperdmx-midi-clip"
DEFAULT_DROP_VELOCITY = 127

# MIME type for scene drops. The payload is a JSON object with the scene's
# name, per-channel value dict and owning fixture type; on drop the timeline
# expands it into one channel-control note_on per channel, sharing a group_id.
SCENE_MIME_TYPE = "application/x-tupperdmx-led-scene"


class ClipListWidget(QListWidget):
    """A list of single-note clips; emits CLIP_MIME_TYPE on drag.

    Each item stores its own ``{"note", "velocity"}`` payload (velocity carries
    the verb's parameter — e.g. a chase index), so the drop/preview is fully
    described by the item with no per-list lookups."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ClipList")
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setIconSize(QSize(16, 16))
        self.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.setUniformItemSizes(True)

    def add_clip(self, label: str, icon: QIcon, note: int, velocity: int,
                 channel: int = 0) -> None:
        item = QListWidgetItem(label)
        item.setIcon(icon)
        item.setData(Qt.ItemDataRole.UserRole, {"note": int(note),
                                                "velocity": int(velocity),
                                                "channel": int(channel)})
        self.addItem(item)

    def mimeData(self, items):
        if not items:
            return None
        payload = items[0].data(Qt.ItemDataRole.UserRole)
        if not isinstance(payload, dict):
            return None
        encoded = json.dumps(payload).encode("utf-8")
        mime = QMimeData()
        mime.setData(CLIP_MIME_TYPE, encoded)
        mime.setText(json.dumps(payload))
        return mime


class SceneListWidget(QListWidget):
    """A list of scenes for one bank; emits SCENE_MIME_TYPE on drag.

    Each item carries the full scene payload (name + per-channel value dict +
    owning fixture type) so the drop handler doesn't need to look anything up.
    On drop, the timeline expands the payload into a channel-control note group.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SceneList")
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setIconSize(QSize(16, 16))
        self.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.setUniformItemSizes(True)

    def mimeData(self, items):
        if not items:
            return None
        payload = items[0].data(Qt.ItemDataRole.UserRole)
        if not isinstance(payload, dict):
            return None
        encoded = json.dumps(payload).encode("utf-8")
        mime = QMimeData()
        mime.setData(SCENE_MIME_TYPE, encoded)
        mime.setText(json.dumps(payload))
        return mime


class ClipLibraryWidget(QWidget):
    """Panel listing draggable clips, grouped from the patched fixtures' defs."""

    # Emitted when an item is clicked. Payload is a list of
    # (channel, note, velocity) tuples — length 1 for a single clip, length N
    # for a scene expanded into its fixture's channel-control notes. The MIDI
    # channel selects the fixture (its patch index), mirroring handle_note_on.
    preview_requested = pyqtSignal(list)

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.setObjectName("ClipLibrary")
        self.config = config
        self.logger = get_logger("ui.clip_library")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.toolbox = QToolBox(self)
        self.toolbox.setObjectName("ClipLibraryToolBox")
        layout.addWidget(self.toolbox)

        # Match the DMX display panel's width policy so the splitter behaves
        # the same regardless of which tab is active.
        self.setMinimumWidth(0)
        self.setMaximumWidth(260)

        self._populate()

    # ------------------------------------------------------------------
    # Population
    # ------------------------------------------------------------------
    def _patched_types(self) -> List[str]:
        """Unique fixture-type names in patch order (deduped)."""
        seen: List[str] = []
        for inst in self.config.fixtures:
            t = getattr(inst, "type", None)
            if t and t not in seen:
                seen.append(t)
        return seen

    def _fixture_index_for_type(self, ftype: str) -> int:
        """Patch index (MIDI channel) of the first patched fixture of
        ``ftype``. The channel selects which fixture handle_note_on targets;
        defaults to 0 if the type isn't patched."""
        for idx, inst in enumerate(self.config.fixtures):
            if getattr(inst, "type", None) == ftype:
                return idx
        return 0

    def _populate(self) -> None:
        """Rebuild the toolbox: per-fixture verb/scene/chase groups (from each
        patched type's def) plus a global show-control group."""
        while self.toolbox.count() > 0:
            widget = self.toolbox.widget(0)
            self.toolbox.removeItem(0)
            if widget is not None:
                widget.deleteLater()

        defs = load_fixture_defs()
        n_groups = 0
        for ftype in self._patched_types():
            fdef = defs.get(ftype)
            if fdef is None:
                continue
            self._add_fixture_page(ftype, fdef)
            self._add_scene_pages(ftype, fdef)
            self._add_chase_page(ftype, fdef)
            n_groups += 1

        self._add_global_page()

        if self.toolbox.count() == 0:
            placeholder = QLabel(
                "No fixtures patched.\n"
                "Patch a fixture (Design stage) or edit the fixture defs."
            )
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setWordWrap(True)
            self.toolbox.addItem(placeholder, "Library")
            return

        self.logger.info(
            f"Clip library populated: {n_groups} fixture type(s), "
            f"{self.toolbox.count()} toolbox section(s) total"
        )

    def _add_fixture_page(self, ftype: str, fdef: FixtureDef) -> None:
        """One page of a fixture's individual verbs: channel controls (labelled
        by channel name) + per-fixture blackout + colour macros."""
        notes: List[int] = list(range(1, fdef.channel_count + 1))
        notes.append(note_layout.BLACKOUT_FIXTURE)
        notes += list(range(note_layout.COLOR_MACRO_BASE,
                            note_layout.COLOR_MACRO_BASE
                            + len(note_layout.COLOR_MACROS)))

        channel = self._fixture_index_for_type(ftype)
        page = ClipListWidget()
        for note in notes:
            info = note_layout.describe_note(note, fdef)
            if info is None:
                continue
            page.add_clip(info.part, self._color_swatch(info.color),
                          note, DEFAULT_DROP_VELOCITY, channel)
        page.itemClicked.connect(self._on_clip_item_clicked)
        self.toolbox.addItem(page, ftype.capitalize())

    def _add_scene_pages(self, ftype: str, fdef: FixtureDef) -> None:
        """One page per scene bank of the fixture (from its def's banks)."""
        scenes = load_scenes(ftype)
        by_bank: Dict[str, List[Scene]] = defaultdict(list)
        for s in scenes:
            by_bank[s.bank].append(s)

        for bank in sorted(by_bank.keys()):
            page = SceneListWidget()
            for scene in sorted(by_bank[bank], key=lambda s: s.index):
                item = QListWidgetItem(scene.name)
                item.setIcon(self._scene_swatch(fdef, scene.values))
                item.setData(Qt.ItemDataRole.UserRole,
                             self._scene_to_payload(scene))
                page.addItem(item)
            page.itemClicked.connect(self._on_scene_item_clicked)
            self.toolbox.addItem(page, f"{ftype.capitalize()}: {bank}")

    def _add_chase_page(self, ftype: str, fdef: FixtureDef) -> None:
        """One page of the fixture's chases (drop as note CHASE_LOAD, velocity
        = chase index)."""
        if not fdef.chases:
            return
        channel = self._fixture_index_for_type(ftype)
        page = ClipListWidget()
        for idx in range(len(fdef.chases)):
            info = note_layout.describe_note(note_layout.CHASE_LOAD, fdef, idx)
            page.add_clip(info.part, self._color_swatch(info.color),
                          note_layout.CHASE_LOAD, idx, channel)
        page.itemClicked.connect(self._on_clip_item_clicked)
        self.toolbox.addItem(page, f"{ftype.capitalize()} chases")

    def _add_global_page(self) -> None:
        """Show-control verbs that route globally (no fixture): START, the song
        parts, and blackout-all."""
        notes = ([note_layout.START]
                 + list(range(note_layout.SONG_PART_BASE,
                              note_layout.SONG_PART_BASE
                              + len(note_layout.SONG_PARTS)))
                 + [note_layout.BLACKOUT_ALL])
        page = ClipListWidget()
        for note in notes:
            info = note_layout.describe_note(note)
            if info is None:
                continue
            page.add_clip(info.part, self._color_swatch(info.color),
                          note, DEFAULT_DROP_VELOCITY)
        page.itemClicked.connect(self._on_clip_item_clicked)
        self.toolbox.addItem(page, "Show")

    # ------------------------------------------------------------------
    # Payload + swatches
    # ------------------------------------------------------------------
    @staticmethod
    def _scene_to_payload(scene: Scene) -> Dict:
        return {
            "name": scene.name,
            "channels": dict(scene.values),
            "fixture_type": scene.fixture_type,
        }

    def _scene_swatch(self, fdef: FixtureDef, values: Dict[str, int]) -> QIcon:
        """Colour swatch from a scene's per-channel values (role-aware). Scenes
        whose RGB channels are all the 255 palette sentinel get a small star,
        because they trigger the controller's live-colour passthrough."""
        r, g, b = note_layout.scene_rgb(fdef, values)
        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setBrush(QColor(r, g, b))
        painter.setPen(QColor(0, 0, 0, 80))
        painter.drawRect(0, 0, 15, 15)
        if self._is_palette_sentinel(fdef, values):
            painter.setPen(QColor(0, 0, 0))
            font = painter.font()
            font.setPointSize(8)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(2, 13, "*")
        painter.end()
        return QIcon(pixmap)

    @staticmethod
    def _is_palette_sentinel(fdef: FixtureDef, values: Dict[str, int]) -> bool:
        rgb = [ch.field for ch in fdef.channels
               if ch.role in (ROLE_RED, ROLE_GREEN, ROLE_BLUE)]
        return len(rgb) == 3 and all(int(values.get(f, 0)) >= 254 for f in rgb)

    def _color_swatch(self, hex_color: str) -> QIcon:
        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setBrush(QColor(hex_color))
        painter.setPen(QColor(0, 0, 0, 80))
        painter.drawRect(0, 0, 15, 15)
        painter.end()
        return QIcon(pixmap)

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------
    def refresh_configuration(self, config: Config) -> None:
        """Refresh the library when configuration changes."""
        self.config = config
        self._populate()
        self.logger.info("Clip library refreshed with new configuration")

    def _on_clip_item_clicked(self, item: QListWidgetItem) -> None:
        """Auditioning a single clip emits its (channel, note, velocity). The
        channel is the clip's home fixture (its patch index) for per-fixture
        pages, and 0 for the channel-agnostic global show verbs."""
        payload = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(payload, dict):
            return
        self.preview_requested.emit([(int(payload.get("channel", 0)),
                                      int(payload["note"]),
                                      int(payload["velocity"]))])

    def _on_scene_item_clicked(self, item: QListWidgetItem) -> None:
        """Auditioning a scene emits one (channel, note, velocity) per channel
        of the owning fixture — mirroring how a scene drop is encoded on the
        timeline. The MIDI channel selects the fixture (its patch index), so the
        scene previews on the right fixture instead of the default channel 0.
        """
        payload = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(payload, dict):
            return
        channels = payload.get("channels") or {}
        fixture_type = payload.get("fixture_type")
        fdef = load_fixture_defs().get(fixture_type)
        if not isinstance(channels, dict) or fdef is None:
            return
        channel = self._fixture_index_for_type(fixture_type)
        messages: List[Tuple[int, int, int]] = []
        for ch_name, note in note_layout.field_note_map(fdef):
            if ch_name not in channels:
                continue
            # Same lossy 0..255 → 0..127 mapping the controller doubles back.
            velocity = max(0, min(127, int(channels[ch_name]) // 2))
            messages.append((channel, note, velocity))
        if messages:
            self.preview_requested.emit(messages)
