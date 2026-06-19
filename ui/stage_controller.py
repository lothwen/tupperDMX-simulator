import os
import sys
from typing import List, Optional
from PyQt6.QtWidgets import QGraphicsView, QGraphicsScene, QWidget, QGraphicsPixmapItem
from PyQt6.QtGui import QPixmap, QPainter
from PyQt6.QtCore import Qt, QObject, pyqtSignal, QRectF

from tupperlogger import get_logger
from config import Config, FixtureInstance, FixturePosition
from fixtures import LedBar, Blinder, GenericFixture
from fixture_library import load_fixture_defs, role_index_map


# Fallback scene size when no background pixmap is loaded; matches the native
# resolution of the bundled stage_background.png so existing fixture coords
# remain valid.
DEFAULT_SCENE_WIDTH = 1536
DEFAULT_SCENE_HEIGHT = 1024


# Render dispatch lives in StageController._make_render_objs: the two built-in
# types keep their bespoke classes (LedBar / Blinder); every other (library)
# type renders via the data-driven fixtures.GenericFixture.


class StageView(QGraphicsView):
    """Graphics view that fits the entire scene proportionally with letterbox."""

    def __init__(self, scene, pixmap, parent=None):
        super().__init__(scene, parent)
        self.pixmap = pixmap
        self._overlay_widget: Optional[QWidget] = None
        self._background_item: Optional[QGraphicsPixmapItem] = None

        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )

        self._install_background(pixmap)

    def _install_background(self, pixmap: QPixmap) -> None:
        scene = self.scene()
        if not pixmap.isNull():
            self._background_item = QGraphicsPixmapItem(pixmap)
            self._background_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
            self._background_item.setZValue(-1000)
            scene.addItem(self._background_item)
            scene.setSceneRect(0, 0, pixmap.width(), pixmap.height())
        else:
            scene.setSceneRect(0, 0, DEFAULT_SCENE_WIDTH, DEFAULT_SCENE_HEIGHT)

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self.pixmap = pixmap
        scene = self.scene()
        if pixmap.isNull():
            if self._background_item is not None:
                scene.removeItem(self._background_item)
                self._background_item = None
            scene.setSceneRect(0, 0, DEFAULT_SCENE_WIDTH, DEFAULT_SCENE_HEIGHT)
        else:
            if self._background_item is None:
                self._background_item = QGraphicsPixmapItem(pixmap)
                self._background_item.setTransformationMode(
                    Qt.TransformationMode.SmoothTransformation
                )
                self._background_item.setZValue(-1000)
                scene.addItem(self._background_item)
            else:
                self._background_item.setPixmap(pixmap)
            scene.setSceneRect(0, 0, pixmap.width(), pixmap.height())
        self._fit_scene()

    def set_overlay_widget(self, widget: QWidget) -> None:
        """Attach a widget to float over the top-right corner of the view."""
        self._overlay_widget = widget
        widget.setParent(self.viewport())
        widget.raise_()
        self._reposition_overlay()
        widget.show()

    def _reposition_overlay(self) -> None:
        if self._overlay_widget is None:
            return
        margin = 8
        w = self._overlay_widget
        x = max(0, self.viewport().width() - w.width() - margin)
        y = margin
        w.move(x, y)

    def _fit_scene(self) -> None:
        scene = self.scene()
        if scene is not None:
            self.fitInView(scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_scene()
        self._reposition_overlay()

    def showEvent(self, event):
        super().showEvent(event)
        self._fit_scene()


class StageController(QObject):
    """Manages the stage view and fixture visualization."""
    
    # Signals
    fixture_updated = pyqtSignal(str, object)  # fixture_type, fixture_object
    stage_clicked = pyqtSignal(float, float)  # x, y coordinates
    # Scenario-designer signals (carry the config.FixtureInstance involved).
    fixture_added = pyqtSignal(object)     # FixtureInstance
    fixture_moved = pyqtSignal(object)     # FixtureInstance (position updated)
    fixture_removed = pyqtSignal(object)   # FixtureInstance
    fixture_selected = pyqtSignal(object)  # FixtureInstance or None

    def __init__(self, config: Config, background_image_path: str = None):
        super().__init__()
        self.config = config
        self.logger = get_logger("ui.stage")

        if background_image_path is None:
            if getattr(sys, "frozen", False):
                _base = sys._MEIPASS
            else:
                # __file__ is ui/stage_controller.py; assets/ is one level up
                _base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            background_image_path = os.path.join(_base, "assets", "images", "stage_background.png")

        # Fixture collections
        self.led_bars: List[LedBar] = []
        self.blinders: List[Blinder] = []
        self.generic_fixtures: List[GenericFixture] = []

        # Scenario-designer state: fixtures are only movable/selectable while
        # design mode is on, so live playback rendering is undisturbed.
        self._design_mode = False

        # Setup stage view
        self.scene = QGraphicsScene()
        self.scene.selectionChanged.connect(self._on_selection_changed)
        self.pixmap = QPixmap(background_image_path)
        
        if self.pixmap.isNull():
            self.logger.warning(f"Could not load background image: {background_image_path}")
        else:
            self.logger.info(f"Loaded background image: {background_image_path}")
        
        self.stage_view = StageView(self.scene, self.pixmap)
        
        # Remove borders and frame from the graphics view
        self.stage_view.setFrameStyle(0)  # No frame
        
        # Setup fixtures
        self._setup_fixtures()
        
        self.logger.info("Stage controller initialized")
    
    def get_stage_view(self) -> StageView:
        """Get the stage view widget."""
        return self.stage_view
    
    def _make_render_objs(self, fixture: FixtureInstance) -> List:
        """Build the render object(s) for one fixture instance.

        The two built-in types keep their bespoke classes; everything else
        renders via the data-driven GenericFixture using its library definition.
        Returns [] for an unrenderable/unknown type."""
        if fixture.type == "ledbar":
            objs = []
            for i, bar_config in enumerate(fixture.bars):
                bar = LedBar(bar_config.x, bar_config.y, i)
                bar._bar_index = i
                objs.append(bar)
            return objs
        if fixture.type == "blinder":
            return [Blinder(fixture.x, fixture.y, fixture.multiply_factor)]

        fdef = load_fixture_defs().get(fixture.type)
        if fdef is None:
            return []
        return [GenericFixture(fixture.x, fixture.y, fdef.channel_count, role_index_map(fdef))]

    def _add_render_group(self, fixture: FixtureInstance) -> bool:
        """Create + register the render object(s) for one fixture instance.

        Incremental: existing scene items are left untouched (so adding a fixture
        never tears down items the view may be mid-paint on — a SIGSEGV source).
        Returns False for an unrenderable/unknown type."""
        render_objs = self._make_render_objs(fixture)
        if not render_objs:
            self.logger.warning(f"Unknown fixture type '{fixture.type}', skipping")
            return False

        for obj in render_objs:
            obj._instance = fixture
            obj._move_callback = self._on_fixture_moved
            obj.set_design_mode(self._design_mode)
            self.scene.addItem(obj)
            if isinstance(obj, LedBar):
                self.led_bars.append(obj)
            elif isinstance(obj, Blinder):
                self.blinders.append(obj)
            else:
                self.generic_fixtures.append(obj)
        self._render_groups.append((fixture, render_objs))
        return True

    def _remove_render_group(self, fixture: FixtureInstance) -> None:
        """Detach + drop the render object(s) for one fixture instance.

        Removes only the targeted items (no full-scene teardown). Items are
        detached from the scene before their Python refs drop."""
        for i, (inst, render_objs) in enumerate(self._render_groups):
            if inst is fixture:
                for obj in render_objs:
                    for coll in (self.led_bars, self.blinders, self.generic_fixtures):
                        if obj in coll:
                            coll.remove(obj)
                    self.scene.removeItem(obj)
                del self._render_groups[i]
                return

    def _setup_fixtures(self) -> None:
        """Setup all fixtures on the stage, dispatching by fixture type.

        Builds per-instance render groups (so each fixture is fed its own DMX
        slice in update_fixtures_with_dmx) while populating the flat
        led_bars / blinders / generic_fixtures collections. Each render object
        gets a back-reference to its FixtureInstance and the move callback so the
        scenario designer can persist drags."""
        # (instance, render-object(s)) pairs, in patch order, for DMX routing.
        self._render_groups: List = []

        for fixture in self.config.fixtures:
            self._add_render_group(fixture)

        total_fixtures = len(self.led_bars) + len(self.blinders) + len(self.generic_fixtures)
        self.logger.info(f"Setup {total_fixtures} fixtures on stage")

    def update_fixtures_with_dmx(self, dmx_data: bytearray) -> None:
        """Update all fixtures with new DMX data, slicing per fixture instance."""
        for fixture, render_objs in self._render_groups:
            dmx_start = fixture.dmx_start - 1
            for obj in render_objs:
                dmx_end = dmx_start + obj.dmx_channel_count
                obj.update_dmx(dmx_data[dmx_start:dmx_end])

        # Update the scene
        self.scene.update()

        # Commented because is too verbose
        # self.logger.debug(f"Updated fixtures with DMX data ({len(dmx_data)} bytes)")
    
    # ------------------------------------------------------------------
    # Scenario designer
    # ------------------------------------------------------------------
    def set_design_mode(self, enabled: bool) -> None:
        """Toggle fixture move/select. Off restores normal playback behaviour."""
        self._design_mode = enabled
        for obj in self.get_all_fixtures():
            obj.set_design_mode(enabled)
        if not enabled:
            self.scene.clearSelection()
        self.logger.info(f"Stage design mode {'enabled' if enabled else 'disabled'}")

    def add_fixture(self, type_name: str, dmx_start: int, x: int, y: int) -> FixtureInstance:
        """Append a new fixture instance to the patch and render it incrementally."""
        instance = FixtureInstance(type=type_name, dmx_start=int(dmx_start),
                                   x=int(x), y=int(y))
        # A ledbar renders from bars[]; seed a single bar at the drop point.
        if type_name == "ledbar" and not instance.bars:
            instance.bars = [FixturePosition(x=int(x), y=int(y))]
        self.config.fixtures.append(instance)
        self._add_render_group(instance)
        self.fixture_added.emit(instance)
        self.logger.info(f"Added fixture '{type_name}' at DMX {dmx_start}")
        return instance

    def remove_selected(self) -> List[FixtureInstance]:
        """Remove the instance(s) backing the current selection (incrementally)."""
        instances: List[FixtureInstance] = []
        for item in self.scene.selectedItems():
            inst = getattr(item, "_instance", None)
            if inst is not None and inst not in instances:
                instances.append(inst)
        for inst in instances:
            self._remove_render_group(inst)
            if inst in self.config.fixtures:
                self.config.fixtures.remove(inst)
        for inst in instances:
            self.fixture_removed.emit(inst)
        return instances

    def _on_fixture_moved(self, obj) -> None:
        """Persist a drag-release onto the fixture's FixtureInstance (x/y, or the
        moved bar for a multi-bar ledbar)."""
        inst = getattr(obj, "_instance", None)
        if inst is None:
            return
        pos = obj.pos()
        bar_index = getattr(obj, "_bar_index", None)
        if bar_index is not None and 0 <= bar_index < len(inst.bars):
            inst.bars[bar_index].x = int(pos.x())
            inst.bars[bar_index].y = int(pos.y())
        else:
            inst.x = int(pos.x())
            inst.y = int(pos.y())
        self.fixture_moved.emit(inst)

    def _on_selection_changed(self) -> None:
        selected = self.scene.selectedItems()
        inst = getattr(selected[0], "_instance", None) if selected else None
        self.fixture_selected.emit(inst)


    def remove_fixture(self, fixture) -> bool:
        """Remove a fixture from the stage."""
        removed = False
        
        if isinstance(fixture, LedBar) and fixture in self.led_bars:
            self.led_bars.remove(fixture)
            self.scene.removeItem(fixture)
            removed = True
            self.logger.info("Removed LED bar from stage")
            
        elif isinstance(fixture, Blinder) and fixture in self.blinders:
            self.blinders.remove(fixture)
            self.scene.removeItem(fixture)
            removed = True
            self.logger.info("Removed blinder from stage")
        
        if removed:
            self.fixture_updated.emit("removed", fixture)
        
        return removed
    
    def clear_all_fixtures(self) -> None:
        """Remove all fixtures from the stage."""
        for obj in self.led_bars + self.blinders + self.generic_fixtures:
            self.scene.removeItem(obj)
        self.led_bars.clear()
        self.blinders.clear()
        self.generic_fixtures.clear()
        self._render_groups = []

        self.logger.info("Cleared all fixtures from stage")
    
    def get_fixture_at_position(self, x: float, y: float, tolerance: float = 10.0):
        """Get fixture at or near the specified position."""
        # Check LED bars
        for bar in self.led_bars:
            bar_pos = bar.pos()
            if (abs(bar_pos.x() - x) < tolerance and 
                abs(bar_pos.y() - y) < tolerance):
                return bar
        
        # Check blinders
        for blinder in self.blinders:
            blinder_pos = blinder.pos()
            if (abs(blinder_pos.x() - x) < tolerance and 
                abs(blinder_pos.y() - y) < tolerance):
                return blinder
        
        return None
    
    def get_all_fixtures(self) -> List:
        """Get list of all fixtures on the stage."""
        return self.led_bars + self.blinders + self.generic_fixtures
    
    def get_led_bars(self) -> List[LedBar]:
        """Get list of LED bar fixtures."""
        return self.led_bars.copy()
    
    def get_blinders(self) -> List[Blinder]:
        """Get list of blinder fixtures."""
        return self.blinders.copy()
    
    def refresh_from_config(self, config: Config) -> None:
        """Refresh fixtures from updated configuration."""
        self.config = config
        self.clear_all_fixtures()
        self._setup_fixtures()
        self.logger.info("Stage refreshed from configuration")
    
    def set_background_image(self, image_path: str) -> bool:
        """Set a new background image for the stage."""
        new_pixmap = QPixmap(image_path)
        
        if new_pixmap.isNull():
            self.logger.warning(f"Failed to load background image: {image_path}")
            return False
        
        self.pixmap = new_pixmap
        self.stage_view.set_pixmap(new_pixmap)

        self.logger.info(f"Background image updated: {image_path}")
        return True
    
    def export_stage_image(self, output_path: str) -> bool:
        """Export the current stage view as an image."""
        try:
            scene_rect = self.scene.itemsBoundingRect()
            pixmap = QPixmap(scene_rect.size().toSize())
            pixmap.fill(Qt.GlobalColor.transparent)
            
            from PyQt6.QtGui import QPainter
            painter = QPainter(pixmap)
            self.scene.render(painter, source=scene_rect)
            painter.end()
            
            success = pixmap.save(output_path)
            if success:
                self.logger.info(f"Stage exported to: {output_path}")
            else:
                self.logger.error(f"Failed to export stage to: {output_path}")
            
            return success
            
        except Exception as e:
            self.logger.error(f"Error exporting stage image: {e}")
            return False