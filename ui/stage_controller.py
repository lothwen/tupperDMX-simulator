import os
import sys
from typing import List, Optional
from PyQt6.QtWidgets import QGraphicsView, QGraphicsScene, QWidget, QGraphicsPixmapItem
from PyQt6.QtGui import QPixmap, QPainter
from PyQt6.QtCore import Qt, QObject, pyqtSignal, QRectF

from tupperlogger import get_logger
from config import Config
from fixtures import LedBar, Blinder


# Fallback scene size when no background pixmap is loaded; matches the native
# resolution of the bundled stage_background.png so existing fixture coords
# remain valid.
DEFAULT_SCENE_WIDTH = 1536
DEFAULT_SCENE_HEIGHT = 1024


# Fixture-type name -> render class. config.fixtures references a type by name;
# the renderer resolves it here. DMX channel count comes from the render class.
# (A real shared/importable fixture library is subtask 3.)
FIXTURE_RENDER = {
    "ledbar": LedBar,
    "blinder": Blinder,
}


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

        # Setup stage view
        self.scene = QGraphicsScene()
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
    
    def _setup_fixtures(self) -> None:
        """Setup all fixtures on the stage, dispatching by fixture type.

        Builds per-instance render groups (so each fixture is fed its own DMX
        slice in update_fixtures_with_dmx) while still populating the flat
        self.led_bars / self.blinders collections the helper methods use."""
        # (instance, render-object(s)) pairs, in patch order, for DMX routing.
        self._render_groups: List = []

        for fixture in self.config.fixtures:
            if fixture.type not in FIXTURE_RENDER:
                self.logger.warning(f"Unknown fixture type '{fixture.type}', skipping")
                continue

            if fixture.type == "ledbar":
                bars = []
                for i, bar_config in enumerate(fixture.bars):
                    bar = LedBar(bar_config.x, bar_config.y, i)
                    bars.append(bar)
                    self.led_bars.append(bar)
                    self.scene.addItem(bar)
                self._render_groups.append((fixture, bars))
            elif fixture.type == "blinder":
                blinder = Blinder(fixture.x, fixture.y, fixture.multiply_factor)
                self.blinders.append(blinder)
                self.scene.addItem(blinder)
                self._render_groups.append((fixture, [blinder]))

        total_fixtures = len(self.led_bars) + len(self.blinders)
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
    
    def add_led_bar(self, x: float, y: float, index: int = None) -> LedBar:
        """Add a new LED bar to the stage."""
        if index is None:
            index = len(self.led_bars)
        
        bar = LedBar(x, y, index)
        self.led_bars.append(bar)
        self.scene.addItem(bar)
        
        self.fixture_updated.emit("led_bar", bar)
        self.logger.info(f"Added LED bar at ({x}, {y})")
        
        return bar
    
    def add_blinder(self, x: float, y: float, multiply_factor: float = 2.0) -> Blinder:
        """Add a new blinder to the stage."""
        blinder = Blinder(x, y, multiply_factor)
        self.blinders.append(blinder)
        self.scene.addItem(blinder)
        
        self.fixture_updated.emit("blinder", blinder)
        self.logger.info(f"Added blinder at ({x}, {y}) with multiply_factor {multiply_factor}")
        
        return blinder
    
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
        # Remove LED bars
        for bar in self.led_bars[:]:
            self.scene.removeItem(bar)
        self.led_bars.clear()
        
        # Remove blinders
        for blinder in self.blinders[:]:
            self.scene.removeItem(blinder)
        self.blinders.clear()
        
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
        return self.led_bars + self.blinders
    
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