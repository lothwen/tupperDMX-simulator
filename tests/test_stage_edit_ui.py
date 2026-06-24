"""
Characterization test: the stage edit-mode UI wiring.

Covers the pieces added when the designer trigger moved out of the hamburger
menu: StageView's multi-overlay support + designer grid flag, the top-left
StageEditToolbar (add / done + drag persistence), and the per-selection
FixtureInspector (name / type / DMX / delete). Mutations funnel through
``on_patch_changed`` so the host persists the patch; ``on_done`` leaves edit mode.

Offscreen Qt + a shared QApplication come from conftest.py (the `qapp` fixture).

Run with:
    uv run python tests/test_stage_edit_ui.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PyQt6.QtWidgets import QWidget, QPushButton
from config import Config, FixtureInstance, FixturePosition


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _stage():
    from ui.stage_controller import StageController
    cfg = Config()
    cfg.fixtures = [
        FixtureInstance(type="ledbar", dmx_start=1, bars=[FixturePosition(150, 350)]),
        FixtureInstance(type="blinder", dmx_start=33, x=575, y=500),
    ]
    return StageController(cfg), cfg


def test_overlay_registration_and_grid(qapp):
    print("test_overlay_registration_and_grid")
    sc, _ = _stage()
    view = sc.get_stage_view()

    w = QWidget()
    view.add_overlay(w, anchor="top-left", reference="background")
    _assert(w.parent() is view.viewport(), "overlay is reparented onto the viewport")
    _assert(any(o[0] is w for o in view._overlays), "overlay is registered")

    view.reposition_overlays()  # must not raise even before the view is shown
    _assert(view._background_rect().__class__.__name__ == "QRect",
            "background rect maps to viewport coordinates")

    _assert(view._grid_visible is False, "grid starts hidden")
    view.set_grid_visible(True)
    _assert(view._grid_visible is True, "set_grid_visible(True) turns the grid on")
    view.set_grid_visible(False)
    _assert(view._grid_visible is False, "set_grid_visible(False) turns it back off")


def test_toolbar_move_persists(qapp):
    print("test_toolbar_move_persists")
    from ui.fixture_designer import StageEditToolbar
    sc, cfg = _stage()
    calls = {"patch": 0}
    StageEditToolbar(sc, lambda: calls.__setitem__("patch", calls["patch"] + 1),
                     lambda: None)

    # A drag-release on the stage must persist through on_patch_changed.
    sc.fixture_moved.emit(cfg.fixtures[1])
    _assert(calls["patch"] == 1, "a fixture move notifies the host to persist")


def test_inspector_populates_and_edits(qapp):
    print("test_inspector_populates_and_edits")
    from ui.fixture_designer import FixtureInspector
    sc, cfg = _stage()
    calls = {"patch": 0}
    insp = FixtureInspector(sc, lambda: calls.__setitem__("patch", calls["patch"] + 1))

    blinder = cfg.fixtures[1]
    insp.set_instance(blinder)
    _assert(insp._type_value.text() == "blinder", "type field shows the fixture type")
    _assert(insp._dmx_spin.value() == 33, "DMX field shows the start address")
    _assert(insp._name_edit.text() == "", "name field starts empty for an unnamed fixture")

    insp._name_edit.setText("Back Right")
    insp._name_edit.editingFinished.emit()
    _assert(blinder.name == "Back Right", "editing the name writes it onto the instance")
    _assert(calls["patch"] == 1, "a name edit notifies the host to persist")

    insp._dmx_spin.setValue(40)
    _assert(blinder.dmx_start == 40, "editing the DMX address writes it onto the instance")
    _assert(calls["patch"] == 2, "a DMX edit notifies the host to persist")


def _click_delete_with_confirmation(insp, answer):
    """Click the inspector's Delete with QMessageBox.question stubbed to ``answer``."""
    from PyQt6.QtWidgets import QMessageBox
    orig = QMessageBox.question
    QMessageBox.question = lambda *a, **k: answer
    try:
        insp.findChild(QPushButton, "InspectorDelete").click()
    finally:
        QMessageBox.question = orig


def test_inspector_delete_removes_and_persists(qapp):
    print("test_inspector_delete_removes_and_persists")
    from ui.fixture_designer import FixtureInspector
    from PyQt6.QtWidgets import QMessageBox
    sc, cfg = _stage()
    calls = {"patch": 0}
    insp = FixtureInspector(sc, lambda: calls.__setitem__("patch", calls["patch"] + 1))

    inst = sc.add_fixture("par_rgb", 9, 400, 300)
    sc.set_design_mode(True)
    sc.generic_fixtures[0].setSelected(True)
    insp.set_instance(inst)

    _click_delete_with_confirmation(insp, QMessageBox.StandardButton.Yes)
    _assert(inst not in cfg.fixtures, "confirmed delete removes the instance from the patch")
    _assert(calls["patch"] == 1, "delete notifies the host to persist")


def test_inspector_delete_cancel_keeps_fixture(qapp):
    print("test_inspector_delete_cancel_keeps_fixture")
    from ui.fixture_designer import FixtureInspector
    from PyQt6.QtWidgets import QMessageBox
    sc, cfg = _stage()
    calls = {"patch": 0}
    insp = FixtureInspector(sc, lambda: calls.__setitem__("patch", calls["patch"] + 1))

    inst = sc.add_fixture("par_rgb", 9, 400, 300)
    sc.set_design_mode(True)
    sc.generic_fixtures[0].setSelected(True)
    insp.set_instance(inst)

    _click_delete_with_confirmation(insp, QMessageBox.StandardButton.No)
    _assert(inst in cfg.fixtures, "cancelling the confirmation keeps the fixture")
    _assert(calls["patch"] == 0, "a cancelled delete does not persist")


def test_inspector_close_clears_selection(qapp):
    print("test_inspector_close_clears_selection")
    from ui.fixture_designer import FixtureInspector
    sc, _ = _stage()
    insp = FixtureInspector(sc, lambda: None)

    sc.add_fixture("par_rgb", 9, 400, 300)
    sc.set_design_mode(True)
    sc.generic_fixtures[0].setSelected(True)
    _assert(len(sc.scene.selectedItems()) == 1, "a fixture is selected")

    insp.findChild(QPushButton, "InspectorClose").click()
    _assert(len(sc.scene.selectedItems()) == 0, "close clears the stage selection")


def test_fixture_labels(qapp):
    print("test_fixture_labels")
    sc, cfg = _stage()

    # Every fixture gets a label item, hidden until design mode is on.
    for obj in sc.get_all_fixtures():
        _assert(obj._label is not None, "each fixture gets a name/DMX label item")
        _assert(not obj._label.isVisible(), "labels are hidden outside design mode")

    sc.set_design_mode(True)
    blinder = sc.blinders[0]
    _assert(blinder._label.isVisible(), "labels show in design mode")
    _assert("blinder" in blinder._label._text and "33" in blinder._label._text,
            "an unnamed fixture's label falls back to type + DMX address")

    # An inspector edit (name) is reflected after refresh_labels().
    cfg.fixtures[1].name = "Back Right"
    sc.refresh_labels()
    _assert("Back Right" in blinder._label._text and "33" in blinder._label._text,
            "the label shows the fixture alias once set")

    sc.set_design_mode(False)
    _assert(not blinder._label.isVisible(), "labels hide again when leaving design mode")


def test_label_follows_fixture(qapp):
    print("test_label_follows_fixture")
    sc, _ = _stage()
    obj = sc.blinders[0]
    before = obj._label.pos()
    obj.setPos(obj.pos().x() + 50, obj.pos().y() + 20)
    after = obj._label.pos()
    _assert(after.x() == before.x() + 50 and after.y() == before.y() + 20,
            "the label tracks the fixture as it moves")


def test_label_removed_with_fixture(qapp):
    print("test_label_removed_with_fixture")
    sc, _ = _stage()
    inst = sc.add_fixture("par_rgb", 9, 400, 300)
    obj = sc.generic_fixtures[0]
    label = obj._label
    _assert(label in sc.scene.items(), "the new fixture's label is in the scene")

    sc.set_design_mode(True)
    obj.setSelected(True)
    sc.remove_selected()
    _assert(label not in sc.scene.items(), "deleting a fixture removes its label too")


def test_toolbar_done_invokes_callback(qapp):
    print("test_toolbar_done_invokes_callback")
    from ui.fixture_designer import StageEditToolbar
    sc, _ = _stage()
    calls = {"done": 0}
    tb = StageEditToolbar(sc, lambda: None, lambda: calls.__setitem__("done", calls["done"] + 1))

    tb.findChild(QPushButton, "EditToolbarDone").click()
    _assert(calls["done"] == 1, "the Done button exits edit mode via on_done")


def main():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    test_overlay_registration_and_grid(app)
    test_toolbar_move_persists(app)
    test_inspector_populates_and_edits(app)
    test_inspector_delete_removes_and_persists(app)
    test_inspector_delete_cancel_keeps_fixture(app)
    test_inspector_close_clears_selection(app)
    test_fixture_labels(app)
    test_label_follows_fixture(app)
    test_label_removed_with_fixture(app)
    test_toolbar_done_invokes_callback(app)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
