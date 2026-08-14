"""
Characterization test: undocking the stage into its own window.

Covers the two stage-side halves the feature rests on — StageView laying its
overlays out as ROWS per corner (so the dock button and "Edit stage" can share
one), and the reversible GPU light (suspend before a reparent, resume after),
which exists because a reparented QOpenGLWidget comes back with a new context
the cached LightLayer handles no longer belong to.

The MainWindow half is not reachable here (nothing in the suite builds the real
window) and neither is the GL half (the offscreen platform refuses
QOpenGLWidget, so `light_layer` is always None) — those are covered end-to-end.

Offscreen Qt + a shared QApplication come from conftest.py (the `qapp` fixture).

Run with:
    uv run python tests/test_stage_undock.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PyQt6.QtWidgets import QVBoxLayout, QWidget
from core.config import Config, FixtureInstance, FixturePosition


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _stage():
    from stage.controller import StageController
    cfg = Config()
    cfg.fixtures = [
        FixtureInstance(type="ledbar", dmx_start=1, bars=[FixturePosition(150, 350)]),
        FixtureInstance(type="blinder", dmx_start=33, x=575, y=500),
    ]
    return StageController(cfg), cfg


def _overlay(width=80, height=30):
    w = QWidget()
    w.resize(width, height)
    return w


def test_same_corner_overlays_form_a_row(qapp):
    print("test_same_corner_overlays_form_a_row")
    sc, _ = _stage()
    view = sc.get_stage_view()
    view.resize(800, 600)

    first, second = _overlay(), _overlay()
    view.add_overlay(first, anchor="top-right", reference="background")
    view.add_overlay(second, anchor="top-right", reference="background")
    first.show()
    second.show()
    view.reposition_overlays()

    _assert(not first.geometry().intersects(second.geometry()),
            "two top-right overlays do not overlap")
    _assert(second.geometry().right() < first.geometry().left(),
            "the later registration sits to the LEFT of the earlier one")
    rect = view._background_rect()
    _assert(second.geometry().left() >= rect.left(),
            "the row stays inside the stage image")


def test_hidden_overlay_takes_no_room(qapp):
    print("test_hidden_overlay_takes_no_room")
    sc, _ = _stage()
    view = sc.get_stage_view()
    view.resize(800, 600)

    first, second = _overlay(), _overlay()
    view.add_overlay(first, anchor="top-right", reference="background")
    view.add_overlay(second, anchor="top-right", reference="background")
    first.show()
    second.show()
    view.reposition_overlays()
    shifted = second.x()

    # This is what edit mode does: "Edit stage" hides, its neighbour must slide
    # into the corner rather than leave a hole.
    first.hide()
    view.reposition_overlays()
    _assert(second.x() > shifted, "the visible overlay moves back toward the corner")
    _assert(second.geometry().right() <= view._background_rect().right(),
            "and stays inside the stage image")


def test_different_corners_are_independent(qapp):
    print("test_different_corners_are_independent")
    sc, _ = _stage()
    view = sc.get_stage_view()
    view.resize(800, 600)

    right, left = _overlay(), _overlay()
    view.add_overlay(right, anchor="top-right", reference="background")
    view.add_overlay(left, anchor="top-left", reference="background")
    right.show()
    left.show()
    view.reposition_overlays()

    rect = view._background_rect()
    _assert(left.x() < right.x(), "each anchor keeps its own corner")
    _assert(abs(left.x() - (rect.left() + 8)) <= 1,
            "the top-left overlay is not pushed by the top-right row")


def test_overlays_survive_a_reparent(qapp):
    print("test_overlays_survive_a_reparent")
    sc, _ = _stage()
    view = sc.get_stage_view()

    button = _overlay()
    view.add_overlay(button, anchor="top-right", reference="background")

    docked = QWidget()
    QVBoxLayout(docked).addWidget(view)
    floating = QWidget()
    floating.setLayout(QVBoxLayout())

    floating.layout().addWidget(view)          # undock
    _assert(view.window() is floating, "the view moved to the other window")
    _assert(button.parent() is view.viewport(),
            "the overlay travelled with the view (it is a viewport child)")
    view.reposition_overlays()                 # must not raise

    docked.layout().addWidget(view)            # dock back
    _assert(view.window() is docked, "the view came back")
    _assert(button.parent() is view.viewport(), "the overlay came back with it")
    view.reposition_overlays()


def test_gpu_suspend_resume_without_gl(qapp):
    print("test_gpu_suspend_resume_without_gl")
    sc, _ = _stage()
    # The offscreen platform refuses QOpenGLWidget, so this is the raster path —
    # both calls must be harmless no-ops rather than raise on the way past.
    _assert(sc.light_layer is None, "no GPU light layer offscreen")

    sc.suspend_gpu_light()
    _assert(sc.light_layer is None, "suspend leaves the raster path alone")
    sc.resume_gpu_light()
    _assert(sc.light_layer is None, "resume does not conjure a layer")
    _assert(sc._gpu_suspended is False, "nothing is owed a resume")


def test_detach_restores_a_raster_viewport(qapp):
    print("test_detach_restores_a_raster_viewport")
    sc, _ = _stage()
    view = sc.get_stage_view()

    button = _overlay()
    view.add_overlay(button, anchor="top-right", reference="background")
    view._light_layer = object()          # pretend a layer is attached

    view.detach_light_layer()
    _assert(view._light_layer is None, "the layer reference is dropped")
    _assert(type(view.viewport()) is QWidget, "the viewport is a plain widget again")
    _assert(button.parent() is view.viewport(),
            "overlays are re-adopted onto the new viewport")


def test_viewport_swap_preserves_overlay_visibility(qapp):
    print("test_viewport_swap_preserves_overlay_visibility")
    sc, _ = _stage()
    view = sc.get_stage_view()

    button, panel = _overlay(), _overlay()
    view.add_overlay(button, anchor="top-right", reference="background")
    view.add_overlay(panel, anchor="top-left", reference="background")
    button.show()
    panel.hide()          # what the edit toolbar and the inspector do at rest

    view._light_layer = object()
    view.detach_light_layer()

    _assert(not button.isHidden(), "the shown overlay stays shown")
    _assert(panel.isHidden(),
            "the HIDDEN overlay stays hidden — a viewport swap must not pop "
            "the stage designer open")


def main():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    test_same_corner_overlays_form_a_row(app)
    test_hidden_overlay_takes_no_room(app)
    test_different_corners_are_independent(app)
    test_overlays_survive_a_reparent(app)
    test_gpu_suspend_resume_without_gl(app)
    test_detach_restores_a_raster_viewport(app)
    test_viewport_swap_preserves_overlay_visibility(app)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
