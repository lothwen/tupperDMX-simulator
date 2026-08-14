from typing import List, Optional
from PyQt6.QtWidgets import QGraphicsView, QGraphicsScene, QWidget, QGraphicsPixmapItem
from PyQt6.QtGui import QPixmap, QPainter, QPen, QColor, QBrush
from PyQt6.QtCore import Qt, QObject, pyqtSignal, QRectF, QRect, QLineF

from tupperlogger import get_logger
from core.config import Config, FixtureInstance, FixturePosition
from core.paths import asset_path
from stage import (
    CLOCK, Atmosphere, BlinderFixture, PixelBarFixture, body_count,
    body_spacing, renderer_for,
)
from core.fixture_library import resolve_fixture
from core.fixture_state import resolve_state


# Fallback scene size when no background pixmap is loaded; matches the native
# resolution of the bundled stage_background.png so existing fixture coords
# remain valid.
DEFAULT_SCENE_WIDTH = 1536
DEFAULT_SCENE_HEIGHT = 1024

# Spacing (in scene units) between grid lines drawn over the stage while the
# scenario designer is active. Purely a visual aid — no snapping (yet).
GRID_SPACING = 96

# Gap (px) between two overlays sharing a corner; see reposition_overlays.
OVERLAY_GAP = 6

# Horizontal spacing (scene units) between the bars of a freshly added ledbar.

# Render dispatch lives in stage/registry.py: a fixture is drawn by whichever
# renderer claims one of its OFL categories, so no fixture type is named here.


class StageView(QGraphicsView):
    """Graphics view that fits the entire scene proportionally with letterbox."""

    def __init__(self, scene, pixmap, parent=None):
        super().__init__(scene, parent)
        self.pixmap = pixmap
        # Each overlay is (widget, anchor, reference, margin); see add_overlay.
        self._overlays: List[tuple] = []
        self._grid_visible = False
        self._background_item: Optional[QGraphicsPixmapItem] = None
        self._light_layer = None

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

    def add_overlay(self, widget: QWidget, anchor: str = "top-right",
                    reference: str = "viewport", margin: int = 8) -> None:
        """Float a widget over a corner of the view.

        ``anchor`` is ``"top-left"`` / ``"top-right"``; ``reference`` is
        ``"viewport"`` (the full widget) or ``"background"`` (the letterboxed
        stage image). The caller controls visibility — repositioning a hidden
        widget is harmless."""
        widget.setParent(self.viewport())
        widget.raise_()
        self._overlays.append((widget, anchor, reference, margin))
        self.reposition_overlays()

    def _background_rect(self) -> QRect:
        """The stage image's bounds in viewport coordinates (letterbox-aware)."""
        scene_rect = self.scene().sceneRect()
        top_left = self.mapFromScene(scene_rect.topLeft())
        bottom_right = self.mapFromScene(scene_rect.bottomRight())
        return QRect(top_left, bottom_right)

    def reposition_overlays(self) -> None:
        """Lay every overlay out against its corner, in rows.

        Overlays sharing an anchor AND a reference are a row rather than a
        pile: the corner they were all given is one point, so without this the
        dock button would sit underneath "Edit stage". Registration order is
        left-to-right, and a hidden widget takes no space — which is what lets
        edit mode swap one control for another without leaving a gap.
        """
        cursors: dict = {}
        for widget, anchor, reference, margin in self._overlays:
            rect = self._background_rect() if reference == "background" else self.viewport().rect()
            key = (anchor, reference)
            if "right" in anchor:
                x = cursors.get(key, rect.right() - margin) - widget.width()
            else:
                x = cursors.get(key, rect.left() + margin)
            widget.move(x, rect.top() + margin)
            if widget.isHidden():
                continue   # placed for when it is shown, but takes no room
            cursors[key] = (x - OVERLAY_GAP if "right" in anchor
                            else x + widget.width() + OVERLAY_GAP)

    def attach_light_layer(self, layer) -> bool:
        """Draw the stage's light on the GPU, over the scene this view renders.

        The viewport itself becomes the GL surface. That is not a preference —
        a `QOpenGLWidget` parented *into* the viewport renders opaque and hides
        the scene entirely, whatever transparency attributes it is given, so
        this is the only placement that works.

        Returns False if the widget can't be created at all (no GL on this
        platform: the offscreen plugin, for one, refuses outright), leaving the
        view exactly as it was.
        """
        released = None
        try:
            from PyQt6.QtOpenGLWidgets import QOpenGLWidget
            released = self._release_overlays()
            self.setViewport(QOpenGLWidget())
        except Exception as exc:
            if released is not None:
                self._readopt_overlays(released)   # back onto the raster viewport
            from tupperlogger import get_logger
            get_logger("stage").warning(
                "No OpenGL viewport (%s); staying on the raster path", exc)
            return False
        self._light_layer = layer
        # A GL viewport hands us an undefined back buffer every frame, so the
        # letterbox has to be painted rather than assumed clear — otherwise it
        # shows whatever was last in that memory, which reads as the window
        # freezing chunks of some other window around the stage.
        self.setBackgroundBrush(QBrush(QColor(0, 0, 0)))
        self.viewport().setAutoFillBackground(True)

        self._readopt_overlays(released)
        return True

    def _release_overlays(self) -> list:
        """Get the overlays out of the viewport before it is replaced.

        `setViewport` deletes the old viewport **synchronously**, and a deleted
        widget takes its children with it — so overlays cannot be re-adopted
        after the swap, only before it. Parking them on the view itself keeps
        them alive across the two calls.

        Returns which ones were showing, because `setParent` hides a widget and
        only the caller knows which deserve to come back: the edit toolbar and
        the fixture inspector spend most of their life deliberately hidden, and
        re-showing them wholesale pops the designer open over a stage nobody
        asked to edit.
        """
        shown = [not widget.isHidden() for widget, *_ in self._overlays]
        for widget, *_ in self._overlays:
            widget.setParent(self)
        return shown

    def _readopt_overlays(self, shown: list) -> None:
        """Re-parent the overlays onto the CURRENT viewport, as they were."""
        for (widget, *_), was_shown in zip(self._overlays, shown):
            widget.setParent(self.viewport())
            widget.raise_()
            widget.setVisible(was_shown)
        self.reposition_overlays()

    def detach_light_layer(self) -> None:
        """Undo `attach_light_layer`: back to a plain raster viewport.

        The GL viewport is dropped rather than kept, because the only caller
        is about to reparent this view into another window — and a reparented
        `QOpenGLWidget` comes back with a NEW context, while the layer's
        shaders, buffers and FBO all belong to the old one.
        """
        self._light_layer = None
        released = self._release_overlays()
        self.setViewport(QWidget())
        # Keep painting the letterbox black rather than restoring the default
        # brush: black is what the bars are on the raster path anyway, and if
        # the GPU never comes back this viewport is the one we keep.
        self.setBackgroundBrush(QBrush(QColor(0, 0, 0)))
        self._readopt_overlays(released)

    def _scene_to_clip(self):
        """The matrix the light layer draws with.

        `fitInView` letterboxes the scene into the viewport, and native GL
        painting bypasses the QPainter that would otherwise carry that for us,
        so the same mapping is rebuilt here: scene -> viewport pixels -> clip.
        """
        from PyQt6.QtGui import QMatrix4x4
        view = self.viewportTransform()
        width = max(1, self.viewport().width())
        height = max(1, self.viewport().height())

        matrix = QMatrix4x4()
        matrix.translate(-1.0, 1.0)              # clip origin is the centre
        matrix.scale(2.0 / width, -2.0 / height)  # y grows downward on screen
        matrix.translate(view.dx(), view.dy())
        matrix.scale(view.m11(), view.m22())
        return matrix

    def set_grid_visible(self, visible: bool) -> None:
        """Show/hide the designer grid drawn over the stage image."""
        if self._grid_visible == visible:
            return
        self._grid_visible = visible
        self.viewport().update()

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        super().drawForeground(painter, rect)
        self._draw_light(painter)
        if not self._grid_visible:
            return
        area = rect.intersected(self.scene().sceneRect())
        if area.isEmpty():
            return
        pen = QPen(QColor(255, 255, 255, 38))
        pen.setCosmetic(True)  # constant on-screen width regardless of fit scale
        painter.setPen(pen)
        x = area.left() - (area.left() % GRID_SPACING)
        while x <= area.right():
            painter.drawLine(QLineF(x, area.top(), x, area.bottom()))
            x += GRID_SPACING
        y = area.top() - (area.top() % GRID_SPACING)
        while y <= area.bottom():
            painter.drawLine(QLineF(area.left(), y, area.right(), y))
            y += GRID_SPACING

    def _draw_light(self, painter: QPainter) -> None:
        """Hand the surface to the light layer for one HDR pass.

        Native painting on purpose: Qt's own compositing is `SourceOver`, and
        light does not cover what is behind it — it adds to it. Inside this
        block the layer gets `glBlendFunc(GL_ONE, GL_ONE)` and real float
        accumulation, which is the entire reason for the GPU path.
        """
        layer = self._light_layer
        if layer is None:
            return
        # The fog needs the stage's own rectangle — where the floor is, and how
        # big a density map to cover it with. Read per frame rather than cached:
        # swapping the backdrop resizes the scene.
        rect = self.scene().sceneRect()
        layer.scene_rect = (rect.x(), rect.y(), rect.width(), rect.height())

        painter.beginNativePainting()
        try:
            ratio = self.viewport().devicePixelRatio()
            # Confine the light to the stage image. The view letterboxes, and
            # the bars either side are not stage — a glow reaching into them
            # lands on whatever the window is sitting on.
            clip = self._background_rect().intersected(self.viewport().rect())
            device_clip = QRect(int(clip.x() * ratio), int(clip.y() * ratio),
                                int(clip.width() * ratio),
                                int(clip.height() * ratio))
            if not layer.render(self._scene_to_clip(),
                                int(self.viewport().width() * ratio),
                                int(self.viewport().height() * ratio),
                                clip=device_clip):
                self._light_layer = None      # it logged; don't ask again
        finally:
            painter.endNativePainting()

    def _fit_scene(self) -> None:
        scene = self.scene()
        if scene is not None:
            self.fitInView(scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_scene()
        self.reposition_overlays()

    def showEvent(self, event):
        super().showEvent(event)
        self._fit_scene()
        self.reposition_overlays()


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
        self.logger = get_logger("stage")

        if background_image_path is None:
            background_image_path = asset_path("images", "stage_background.png")

        # Fixture collections
        # Every body on the stage, in patch order. The per-kind lists below
        # are views over it — rendering dispatches by category now, so there is
        # nothing to gain from keeping fixtures sorted into type buckets.
        self.render_objs: List = []

        # The air. Cross-fixture by nature: a hazer downstage is what makes a
        # moving head's beam visible on the other side of the rig, so the level
        # belongs to the stage rather than to any body. It drifts on the shared
        # clock — haze builds in seconds and hangs around for the better part of
        # a minute, and a beam that appeared and vanished with the hazer's DMX
        # would read as a bug.
        self.atmosphere = Atmosphere(config.window.stage_haze)
        self._haze_token = CLOCK.subscribe(self._advance_atmosphere)

        # Scenario-designer state: fixtures are only movable/selectable while
        # design mode is on, so live playback rendering is undisturbed.
        self._design_mode = False

        # Setup stage view
        self.scene = QGraphicsScene()
        # Linear item lookup instead of the BSP tree: this scene mutates heavily
        # (fixtures added/removed at runtime; ~20Hz DMX redraws). The BSP index
        # doesn't purge implicitly-removed child items (a bar's pixel cells), so it
        # kept dangling pointers — the view painted ghosts of deleted fixtures,
        # hit-testing found removed items (ungrabMouse-without-scene), and a
        # freed item left the index dangling -> SIGSEGV. NoIndex iterates the
        # live item list; trivial for a few hundred items, hit-tested only on
        # mouse events.
        self.scene.setItemIndexMethod(QGraphicsScene.ItemIndexMethod.NoIndex)
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

        self.light_layer = None
        self._gpu_suspended = False
        if config.window.stage_gpu:
            self._enable_gpu_light()

        self.logger.info("Stage controller initialized")

    # ------------------------------------------------------------------
    # GPU light
    # ------------------------------------------------------------------
    def _enable_gpu_light(self) -> bool:
        """Move the stage's light onto the GPU, or quietly stay as we are."""
        try:
            from stage.gpu.layer import LightLayer
        except Exception as exc:
            self.logger.warning("GPU light layer unavailable (%s)", exc)
            return False

        layer = LightLayer(lambda: self.render_objs)
        layer.exposure = self.config.window.stage_exposure
        layer.haze = self.atmosphere.level
        layer.fog = self.config.window.fog
        # The fog march is dithered per frame and averaged over several of them,
        # so it needs a few repaints after the rig has stopped changing to
        # settle. Nothing else on the stage asks for a repaint it was not given
        # a reason for, so the layer has to ask for these itself — and stop.
        layer.request_repaint = self._on_light_changed
        if not self.stage_view.attach_light_layer(layer):
            return False

        self.light_layer = layer
        for obj in self.render_objs:
            obj._light_sink = self._on_light_changed
            obj.set_light_on_gpu(True)

        # Tell the scene preview it can do the same. Set only once the stage's
        # own context is up, so the preview never gambles on GL the stage has
        # already found to be missing.
        from stage.gpu import layer as gpu_layer
        gpu_layer.STAGE_GPU_ACTIVE = True
        return True

    def suspend_gpu_light(self) -> None:
        """Give up the GPU light so the view can be reparented.

        Reparenting a `QOpenGLWidget` destroys and recreates its context, and
        `LightLayer` caches its functions, programs, VAO, buffers and FBO from
        whichever context it first initialised against — it never revalidates
        them. Worse, both fail-soft paths latch one way (`LightLayer._failed`
        and `StageView._light_layer = None`), so a single bad frame after a
        move would cost the GPU stage for the rest of the session.

        So the layer is thrown away rather than carried across, and `resume`
        builds a new one against the new context. The dead layer's GL objects
        go with the context Qt is about to destroy.
        """
        if self.light_layer is None:
            return
        self._gpu_suspended = True
        self.light_layer = None
        for obj in self.render_objs:
            obj.set_light_on_gpu(False)
            obj._light_sink = None
        self.stage_view.detach_light_layer()
        # Write-once no longer: the scene preview gates its own GL on this, and
        # while the stage has no context the honest answer is False.
        from stage.gpu import layer as gpu_layer
        gpu_layer.STAGE_GPU_ACTIVE = False

    def resume_gpu_light(self) -> None:
        """Rebuild the GPU light after a reparent. Fails soft to raster."""
        if not self._gpu_suspended:
            return
        self._gpu_suspended = False
        self._enable_gpu_light()

    def _on_light_changed(self) -> None:
        """A body's light list changed; ask for one repaint.

        Qt coalesces `update()` per event-loop turn, so a rig where every
        fixture moves costs the same as one where a single fixture does — and a
        rig where nothing moves costs nothing, which is what keeps the idle
        cost where the raster path put it.
        """
        if self.light_layer is not None:
            self.stage_view.viewport().update()
    
    def get_stage_view(self) -> StageView:
        """Get the stage view widget."""
        return self.stage_view
    
    def _make_render_objs(self, fixture: FixtureInstance) -> List:
        """Build the render object(s) for one fixture instance — one per body.

        Which renderer draws it comes from its definition's OFL categories, so
        a fixture type nobody wrote code for still appears correctly. A fixture
        with several bodies (an LED bar's bars) gets one object per entry in
        `bars`, all fed the same DMX slice.

        Returns [] for an unrenderable/unknown type."""
        fdef = resolve_fixture(fixture.type, getattr(fixture, "mode", ""))
        if fdef is None:
            return []

        renderer = renderer_for(fdef)
        # A patch saved before a definition's body count was settled can carry
        # more `bars` than the fixture really has; the definition decides.
        bars = fixture.bars[:body_count(fdef)]
        multi_body = len(bars) > 1
        positions = bars if multi_body else [FixturePosition(fixture.x, fixture.y)]

        objs = []
        for index, position in enumerate(positions):
            obj = renderer(fdef, position.x, position.y, body_index=index,
                           hanging=getattr(fixture, "hanging", True))
            # None for a single-body fixture, so it owns its label and its drag
            # writes back to the instance rather than to a bars[] entry.
            obj._bar_index = index if multi_body else None
            objs.append(obj)
        return objs

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
            obj.set_atmosphere(self.atmosphere)
            obj.set_design_mode(self._design_mode)
            if getattr(self, "light_layer", None) is not None:
                obj._light_sink = self._on_light_changed
                obj.set_light_on_gpu(True)
            self.scene.addItem(obj)
            obj.attach_label(self.scene)
            self.render_objs.append(obj)
        self._render_groups.append((fixture, render_objs))
        return True

    def _remove_render_group(self, fixture: FixtureInstance) -> None:
        """Detach + drop the render object(s) for one fixture instance.

        Removes only the targeted items (no full-scene teardown). Stops each
        object's resources (animation movies, strobe timers) so a removed body
        doesn't keep ticking, then detaches it from the scene before its refs
        drop. Safe freeing relies on the scene's NoIndex item method (see
        __init__)."""
        self._last_slices.pop(id(fixture), None)
        for i, (inst, render_objs) in enumerate(self._render_groups):
            if inst is fixture:
                for obj in render_objs:
                    cleanup = getattr(obj, "cleanup", None)
                    if callable(cleanup):
                        cleanup()
                    if obj in self.render_objs:
                        self.render_objs.remove(obj)
                    obj.remove_label(self.scene)
                    self.scene.removeItem(obj)
                del self._render_groups[i]
                return

    def _setup_fixtures(self) -> None:
        """Setup all fixtures on the stage, dispatching by fixture type.

        Builds per-instance render groups (so each fixture is fed its own DMX
        slice in update_fixtures_with_dmx) while populating the flat
        flat render_objs list. Each render object
        gets a back-reference to its FixtureInstance and the move callback so the
        scenario designer can persist drags."""
        # (instance, render-object(s)) pairs, in patch order, for DMX routing.
        self._render_groups: List = []
        # The last slice each fixture was given, so an unchanged frame is free.
        self._last_slices: dict = {}

        for fixture in self.config.fixtures:
            self._add_render_group(fixture)

        self.logger.info(f"Setup {len(self.render_objs)} fixture bodies on stage")

    def update_fixtures_with_dmx(self, dmx_data: bytearray) -> None:
        """Update all fixtures with new DMX data, slicing per fixture instance.

        The slice is resolved ONCE per fixture and pushed into each of its
        bodies: they mirror the same channels and differ only in which part of
        the fixture they are, so an eight-bar LED set used to resolve the
        identical seven bytes eight times a frame, sixty times a second.

        No scene-wide repaint at the end either — every item that changed has
        already invalidated itself, and with the scene's NoIndex a blanket
        `update()` repaints all 1536x1024 of it whether or not anything moved.
        """
        for fixture, render_objs in self._render_groups:
            if not render_objs:
                continue
            dmx_start = fixture.dmx_start - 1
            dmx_end = dmx_start + render_objs[0].dmx_channel_count
            slice_ = bytes(dmx_data[dmx_start:dmx_end])
            if len(slice_) < render_objs[0].dmx_channel_count:
                continue
            # A fixture holding still sends the same bytes twenty times a
            # second. Anything that moves between frames — a strobe, a spinning
            # gobo — is redrawn by the shared clock instead, so skipping here
            # costs it nothing.
            if self._last_slices.get(id(fixture)) == slice_:
                continue
            self._last_slices[id(fixture)] = slice_

            state = resolve_state(render_objs[0].fdef, slice_)
            for obj in render_objs:
                obj.set_state(state)
    
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

    def refresh_labels(self) -> None:
        """Re-sync every fixture's name/DMX label (e.g. after an inspector edit)."""
        for obj in self.get_all_fixtures():
            obj.refresh_label()

    def add_fixture(self, type_name: str, dmx_start: int, x: int, y: int,
                    channel: int = -1, mode: str = "") -> FixtureInstance:
        """Append a new fixture instance to the patch and render it incrementally.

        `channel` is the fixture's stable MIDI channel (what clips bind to);
        defaults to the lowest free channel when not supplied. `mode` picks the
        fixture's DMX layout; empty means the type's default.

        How it is rigged is *guessed* from where it lands — a fixture dropped
        on the deck stands on it, one dropped above stage centre is flown — and
        then written down, so the guess is only ever a starting point the
        inspector can correct. Guessing it live off the position instead would
        turn a fixture over mid-drag."""
        if channel < 0:
            channel = self.config.next_free_channel()
        instance = FixtureInstance(type=type_name, dmx_start=int(dmx_start),
                                   channel=int(channel), mode=str(mode),
                                   x=int(x), y=int(y),
                                   hanging=self.is_flown_height(y))
        # A fixture sold as several housings on one DMX slice (the ledbar's 8
        # bars) gets a body each; seed them as a horizontal row centred on the
        # drop point.
        fdef = resolve_fixture(type_name, mode)
        count = body_count(fdef) if fdef else 1
        if count > 1 and not instance.bars:
            spacing = body_spacing(fdef)
            start_x = x - spacing * (count - 1) / 2
            instance.bars = [
                FixturePosition(x=int(start_x + i * spacing), y=int(y))
                for i in range(count)
            ]
        self.config.fixtures.append(instance)
        self._add_render_group(instance)
        self.fixture_added.emit(instance)
        self.logger.info(
            f"Added fixture '{type_name}' at DMX {dmx_start} (channel {channel})")
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

    #: Where the deck starts, as a fraction of the stage picture's height.
    #: Not the midpoint: trusses hang over most of a room and the floor package
    #: sits in the bottom of it, and the browser drops a new fixture at the
    #: exact centre — which should land on a truss, since that is where it was
    #: going before the user drags it there.
    DECK_TOP = 0.66

    def is_flown_height(self, y: float) -> bool:
        """Would a fixture dropped at `y` be hung rather than stood on the deck?

        The stage is a picture of a room, so height in it is the only clue
        there is. Only ever consulted when a fixture is added — see
        `add_fixture` — never while one is being dragged.
        """
        rect = self.scene.sceneRect()
        return y < rect.top() + rect.height() * self.DECK_TOP

    def can_rig_inverted(self, instance: FixtureInstance) -> bool:
        """Whether this fixture's profile can be hung upside down at all.

        A smoke machine or a stand always sits on its own feet, so offering to
        turn one over would be offering nothing.
        """
        fdef = resolve_fixture(instance.type, getattr(instance, "mode", ""))
        return bool(fdef) and renderer_for(fdef).rigs_inverted

    def _selected_bodies(self, instance: FixtureInstance) -> List[int]:
        """Which of a fixture's bodies are selected, by position in its group.

        Half of carrying a selection across a rebuild: a multi-body fixture is
        selected one body at a time (that is how a ledbar's bars are dragged
        apart), so it is not enough to know that the fixture was selected.
        """
        for inst, render_objs in self._render_groups:
            if inst is instance:
                return [index for index, obj in enumerate(render_objs)
                        if obj.isSelected()]
        return []

    def _reselect_bodies(self, instance: FixtureInstance,
                         indices: List[int]) -> None:
        """Re-select the bodies a rebuild replaced, so the scene still agrees
        with the inspector about what is selected.

        `QGraphicsScene.removeItem` drops a selected item from the selection
        *without* emitting `selectionChanged`, and the fresh bodies come back
        unselected — so a rebuild left the stage with nothing selected while
        `fixture_selected` had just said otherwise. The fixture lost its
        selection outline, and Close (which clears an already-empty selection,
        emitting nothing) could no longer hide the inspector at all.
        """
        if not indices:
            return
        for inst, render_objs in self._render_groups:
            if inst is instance:
                for index in indices:
                    if index < len(render_objs):
                        render_objs[index].setSelected(True)
                return

    def set_fixture_hanging(self, instance: FixtureInstance,
                            hanging: bool) -> None:
        """Fly a patched fixture or stand it on the deck, rebuilding its bodies.

        Which way up a fixture is rigged is decided when its housing is built
        and read by the aim it is given, so — like a mode change — it is a
        rebuild rather than a repaint.
        """
        if bool(getattr(instance, "hanging", True)) == bool(hanging):
            return
        selected = self._selected_bodies(instance)
        self._remove_render_group(instance)
        instance.hanging = bool(hanging)
        self._add_render_group(instance)
        self._reselect_bodies(instance, selected)
        self.fixture_selected.emit(instance)
        self.logger.info(f"Fixture '{instance.type}' is now "
                         f"{'flown' if hanging else 'standing on the deck'}")

    def change_fixture_mode(self, instance: FixtureInstance, mode: str) -> None:
        """Switch a patched fixture to another mode, rebuilding its render group.

        The mode decides the channel layout, so the render objects have to be
        rebuilt against the new definition."""
        if getattr(instance, "mode", "") == mode:
            return
        selected = self._selected_bodies(instance)
        self._remove_render_group(instance)
        instance.mode = mode
        self._add_render_group(instance)
        self._reselect_bodies(instance, selected)
        self.fixture_selected.emit(instance)
        self.logger.info(f"Fixture '{instance.type}' switched to mode "
                         f"'{mode or '(default)'}'")

    def _advance_atmosphere(self) -> None:
        """Move the haze toward what the patched fog fixtures are asking for,
        and give the air its own frames while it is drifting.

        This runs at 30 Hz for as long as the stage exists — it is the one clock
        subscription that is never conditional — so it must not ask for a
        repaint unless something has actually changed, or a rig standing
        perfectly still would redraw forever.

        Two things can have changed. The haze level, which is the drift a hazer
        coming on produces. And, once the volumetric march is on with any
        texture at all, the air itself: its noise flows with this same clock, so
        the picture really is different every tick even with nothing on stage
        moving. Nobody else can notice that — the light layer's own frame
        signature deliberately has no time in it — so it is driven from here,
        which also paces it at the rate everything else on the stage animates at
        instead of as fast as the compositor will take frames.
        """
        level = self.atmosphere.advance(CLOCK.now())
        layer = self.light_layer
        if layer is None:
            return
        if abs(level - layer.haze) > 1e-4:
            layer.haze = level
            self._on_light_changed()
        elif layer.air_is_drifting():
            self._on_light_changed()

    def set_haze_baseline(self, value: float) -> None:
        """How hazy the room is before any fixture. Persisted with the window.

        Redraws. The air is not something a body is told about — every body
        reads `atmosphere.level` on the frame it happens to be drawing — so
        without this the slider did nothing at all until the next DMX frame
        arrived, and on a stage standing still it did nothing full stop. That
        is what "the haze slider does not work" was.
        """
        before = self.atmosphere.level
        self.atmosphere.set_baseline(value)
        self.config.window.stage_haze = self.atmosphere.baseline
        # Straight to the new level rather than drifting to it: the drift is
        # there so a *hazer* coming on reads as haze filling a room, and nobody
        # dragging a slider wants to wait twelve seconds to see what they did.
        self.atmosphere.settle()
        if self.atmosphere.level != before:
            self.refresh_air()

    def refresh_air(self) -> None:
        """Redraw every body against the air as it is now.

        Both paths need it and for different reasons: on the raster path the
        haze is baked into each cone's own brush, and on the GPU path it is a
        uniform the light layer has to be handed.
        """
        for obj in self.render_objs:
            obj.render()
        if self.light_layer is not None:
            self.light_layer.haze = self.atmosphere.level
            self._on_light_changed()

    def set_exposure(self, value: float) -> None:
        """Exposure before the tonemap. Only the GPU path has one."""
        self.config.window.stage_exposure = max(0.1, min(4.0, float(value)))
        if self.light_layer is not None:
            self.light_layer.exposure = self.config.window.stage_exposure
            self._on_light_changed()

    def apply_stage_config(self) -> None:
        """Re-read the stage settings from the config and redraw.

        Settings **replaces** `config.window.fog` with a new object rather than
        editing the one that is there, so the light layer went on holding the
        old one: ticking "Volumetric fog" did nothing at all until the app was
        restarted, which is not what the dialog says and not what anyone
        expects of a switch. `stage_gpu` genuinely does need a relaunch — there
        is a GL context to build — and the dialog says so.
        """
        if self.light_layer is None:
            return
        self.light_layer.fog = self.config.window.fog
        self.light_layer.exposure = self.config.window.stage_exposure
        self._on_light_changed()

    def set_fog(self, **values) -> None:
        """Edit the volumetric-fog settings and redraw.

        One writer for all of them, because they are all the same kind of thing:
        a field on `config.window.fog` that the light layer reads. Validated
        through the config's own parser rather than by each caller, so a slider
        and a prefs file cannot disagree about what is in range.

        Note there is no `density` here — that is `stage_haze`, which the fog
        pass reads as the amount of medium in clean air. One density control.
        """
        from dataclasses import asdict
        from core.config import Config

        current = asdict(self.config.window.fog)
        current.update(values)
        self.config.window.fog = Config._parse_fog(current)
        if self.light_layer is not None:
            self.light_layer.fog = self.config.window.fog
            self._on_light_changed()

    def _on_selection_changed(self) -> None:
        try:
            selected = self.scene.selectedItems()
        except RuntimeError:
            # A QGraphicsScene clears its selection as it is destroyed, and the
            # connection outlives the C++ object it came from — so this slot can
            # be entered with nothing left to ask. There is no selection to
            # report at that point, and raising here would surface as a stray
            # traceback during shutdown.
            return
        inst = getattr(selected[0], "_instance", None) if selected else None
        self.fixture_selected.emit(inst)


    # -- per-kind views -------------------------------------------------
    # Kept because the designer tests and older call sites ask for fixtures by
    # kind; rendering itself never needs them.
    @property
    def led_bars(self) -> List:
        return [o for o in self.render_objs if isinstance(o, PixelBarFixture)]

    @property
    def blinders(self) -> List:
        return [o for o in self.render_objs if isinstance(o, BlinderFixture)]

    @property
    def generic_fixtures(self) -> List:
        return [o for o in self.render_objs
                if not isinstance(o, (PixelBarFixture, BlinderFixture))]

    def clear_all_fixtures(self) -> None:
        """Remove all fixtures from the stage."""
        for obj in list(self.render_objs):
            obj.cleanup()
            obj.remove_label(self.scene)
            self.scene.removeItem(obj)
        self.render_objs.clear()
        self._render_groups = []
        self.logger.info("Cleared all fixtures from stage")

    def get_all_fixtures(self) -> List:
        """Every fixture body on the stage."""
        return list(self.render_objs)

    def sync_from_config(self, config: Config = None) -> None:
        """Bring the stage in line with the patch, adding and removing per fixture.

        Opening or restoring a show rebinds `config.fixtures` to a whole new
        list; without this the previous show's rig stayed on the stage until
        the app was restarted.

        Incremental on purpose: a fixture that is in both patches keeps the very
        same body, so nothing it owns is torn down while the DMX loop may be
        painting it — the same reason add and remove have always been per
        fixture rather than a rebuild.
        """
        if config is not None:
            self.config = config

        wanted = list(self.config.fixtures)
        current = [instance for instance, _ in self._render_groups]
        by_identity = {id(instance) for instance in wanted}

        for instance in current:
            if id(instance) not in by_identity:
                self._remove_render_group(instance)

        known = {id(instance) for instance, _ in self._render_groups}
        for instance in wanted:
            if id(instance) not in known:
                self._add_render_group(instance)

        # Match the patch's order, so the per-kind views and any index into
        # render_objs still read in the order the user sees.
        order = {id(instance): position for position, instance in enumerate(wanted)}
        self._render_groups.sort(key=lambda pair: order.get(id(pair[0]), 0))
        self.render_objs = [obj for _, objs in self._render_groups for obj in objs]
        self.logger.info(f"Stage synced to {len(self._render_groups)} fixtures")
    
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