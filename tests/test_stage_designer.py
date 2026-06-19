"""
Characterization test: the stage scenario designer's add/remove are incremental.

Adding or removing a fixture must NOT tear down and recreate the whole scene:
a full rebuild deletes items the QGraphicsView may be mid-paint on (while the
~20Hz DMX loop keeps the scene dirty), which segfaults. These tests pin the
incremental contract — existing render objects keep their identity across an add,
and a remove drops only the targeted group — plus the patch/DMX side effects.

Offscreen Qt + a shared QApplication come from conftest.py (the `qapp` fixture).

Run with:
    uv run python tests/test_stage_designer.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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


def test_add_is_incremental(qapp):
    print("test_add_is_incremental")
    sc, cfg = _stage()
    before_ids = {id(o) for o in sc.get_all_fixtures()}

    inst = sc.add_fixture("par_rgb", 9, 400, 300)

    after_ids = {id(o) for o in sc.get_all_fixtures()}
    _assert(before_ids <= after_ids,
            "existing render objects are preserved (not recreated) across an add")
    _assert(len(sc._render_groups) == 3, "a render group was appended")
    _assert(len(sc.generic_fixtures) == 1, "the generic fixture was created")
    _assert(cfg.fixtures[-1] is inst, "the instance was appended to the patch")


def test_added_fixture_receives_dmx(qapp):
    print("test_added_fixture_receives_dmx")
    sc, _ = _stage()
    sc.add_fixture("par_rgb", 9, 400, 300)
    buf = bytearray(96)
    buf[8], buf[9], buf[10], buf[11] = 255, 10, 20, 30  # par_rgb at DMX 9 (index 8)
    sc.update_fixtures_with_dmx(buf)
    c = sc.generic_fixtures[0].bulb.glow_color
    _assert((c.red(), c.green(), c.blue()) == (10, 20, 30),
            "the newly added fixture renders its DMX slice")


def test_remove_drops_only_target(qapp):
    print("test_remove_drops_only_target")
    sc, cfg = _stage()
    inst = sc.add_fixture("par_rgb", 9, 400, 300)
    keep_ids = {id(o) for o in (sc.led_bars + sc.blinders)}

    sc.set_design_mode(True)
    sc.generic_fixtures[0].setSelected(True)
    removed = sc.remove_selected()

    _assert([r.type for r in removed] == ["par_rgb"], "only the selected fixture removed")
    _assert(len(sc.generic_fixtures) == 0, "the generic render object was dropped")
    _assert(len(sc._render_groups) == 2, "its render group was removed")
    _assert(inst not in cfg.fixtures, "the instance was removed from the patch")
    _assert({id(o) for o in (sc.led_bars + sc.blinders)} == keep_ids,
            "the other fixtures' render objects are untouched")


def main():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    test_add_is_incremental(app)
    test_added_fixture_receives_dmx(app)
    test_remove_drops_only_target(app)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
