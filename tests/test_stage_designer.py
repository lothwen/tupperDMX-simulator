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


def test_add_ledbar_seeds_all_bars(qapp):
    print("test_add_ledbar_seeds_all_bars")
    from fixture_library import load_fixture_defs
    sc, _ = _stage()
    count = load_fixture_defs()["ledbar"].bar_count
    _assert(count > 1, "the ledbar type def declares more than one bar")

    inst = sc.add_fixture("ledbar", 9, 768, 350)
    _assert(len(inst.bars) == count,
            f"a new ledbar seeds all {count} bars from its type def")
    # The pre-existing ledbar contributes 1 bar; the new one contributes `count`.
    _assert(len(sc.led_bars) == 1 + count,
            "each seeded bar gets its own LedBar render object")


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


def test_scene_uses_no_index(qapp):
    """The stage scene must use NoIndex item lookup.

    The BSP tree index doesn't purge implicitly-removed child items (a LedBar's
    pixels), leaving dangling pointers: with a live view + ~20Hz redraws this
    painted ghosts of deleted fixtures, made removed items still hit-testable
    (ungrabMouse-without-scene), and made a freed item crash the next paint
    (SIGSEGV). That staleness only manifests with a real view/paint loop, so we
    pin the one-line root-cause fix directly here."""
    print("test_scene_uses_no_index")
    from PyQt6.QtWidgets import QGraphicsScene
    sc, _ = _stage()
    _assert(sc.scene.itemIndexMethod() == QGraphicsScene.ItemIndexMethod.NoIndex,
            "the stage scene disables the BSP index (NoIndex)")


def test_remove_ledbar_purges_bars(qapp):
    """Deleting a ledbar detaches every bar from the scene, stops its animation,
    and drops it from the patch (no leftover bars to ghost or be hit-tested)."""
    print("test_remove_ledbar_purges_bars")
    sc, cfg = _stage()
    led = cfg.fixtures[0]
    # Drive DMX so the bars spin up animation movies (cleanup must stop them).
    buf = bytearray(96)
    buf[0], buf[3], buf[4] = 255, 255, 4  # brightness, red, animation
    sc.update_fixtures_with_dmx(buf)
    bars = list(sc.led_bars)
    _assert(len(bars) == 1, "the ledbar contributes its bar(s) as LedBar objects")

    sc.set_design_mode(True)
    bars[0].setSelected(True)
    removed = sc.remove_selected()

    _assert([r.type for r in removed] == ["ledbar"], "the ledbar instance was removed")
    _assert(len(sc.led_bars) == 0, "its bars left the led_bars collection")
    _assert(led not in cfg.fixtures, "the instance was removed from the patch")
    for b in bars:
        _assert(b.scene() is None, "each removed bar is detached from the scene")
        _assert(b.current_movie is None, "each removed bar's animation movie was stopped")


def main():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    test_add_is_incremental(app)
    test_added_fixture_receives_dmx(app)
    test_add_ledbar_seeds_all_bars(app)
    test_remove_drops_only_target(app)
    test_scene_uses_no_index(app)
    test_remove_ledbar_purges_bars(app)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
