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
    c = sc.generic_fixtures[0].lamp.glow_color
    _assert((c.red(), c.green(), c.blue()) == (10, 20, 30),
            "the newly added fixture renders its DMX slice")


def test_add_ledbar_seeds_all_bars(qapp):
    print("test_add_ledbar_seeds_all_bars")
    from core.fixture_library import load_fixture_defs
    from stage.metrics import body_count
    sc, _ = _stage()
    count = body_count(load_fixture_defs()["ledbar"])
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
    _assert(len(bars) == 1, "the ledbar contributes its bar(s) as pixel-bar render objects")

    sc.set_design_mode(True)
    bars[0].setSelected(True)
    removed = sc.remove_selected()

    _assert([r.type for r in removed] == ["ledbar"], "the ledbar instance was removed")
    _assert(len(sc.led_bars) == 0, "its bars left the led_bars collection")
    _assert(led not in cfg.fixtures, "the instance was removed from the patch")
    for b in bars:
        _assert(b.scene() is None, "each removed bar is detached from the scene")
        _assert(b.current_movie is None,
                    "each removed bar's animation movie was stopped")


def test_remove_then_readd_keeps_other_channels_stable(qapp):
    """The core regression: removing a fixture and adding one back must not
    re-number the surviving fixtures' MIDI channels (their timeline clips bind
    by channel, so a shift would silently rebind them)."""
    print("test_remove_then_readd_keeps_other_channels_stable")
    cfg = Config()
    cfg.fixtures = [
        FixtureInstance(type="ledbar", dmx_start=1, channel=0,
                        bars=[FixturePosition(150, 350)]),
        FixtureInstance(type="blinder", dmx_start=33, channel=1, x=400, y=300),
        FixtureInstance(type="blinder", dmx_start=65, channel=2, x=575, y=500),
    ]
    from stage.controller import StageController
    sc = StageController(cfg)

    # Remove the middle fixture (channel 1) via the real designer path.
    sc.set_design_mode(True)
    sc.blinders[0].setSelected(True)   # the @33 blinder
    removed = sc.remove_selected()
    _assert([r.channel for r in removed] == [1], "the channel-1 fixture was removed")
    _assert(sorted(f.channel for f in cfg.fixtures) == [0, 2],
            "survivors keep channels 0 and 2 (no renumber)")

    # Re-add a blinder: it reclaims the lowest free channel (1), survivors intact.
    inst = sc.add_fixture("blinder", 33, 400, 300)
    _assert(inst.channel == 1, "re-added fixture reclaims the freed channel 1")
    _assert(sorted(f.channel for f in cfg.fixtures) == [0, 1, 2],
            "survivors' channels still untouched after re-add")

    # Gap-insert (DMX 17, between ledbar@1 and blinder@33): fresh channel, no shift.
    gap = sc.add_fixture("par_rgb", 17, 500, 300)
    _assert(gap.channel == 3, "gap-inserted fixture gets a fresh channel (3)")
    _assert(sorted(f.channel for f in cfg.fixtures) == [0, 1, 2, 3],
            "no existing channel shifted by a gap insert")


def test_opening_another_show_replaces_the_fixtures(qapp):
    print("test_opening_another_show_replaces_the_fixtures")
    # Opening a show rebinds config.fixtures to a whole new list. Nothing used
    # to tell the stage, so the previous show's rig stayed lit until restart.
    stage, cfg = _stage()
    before = list(stage.render_objs)
    _assert(len(stage._render_groups) == 2, "the first show's patch is on stage")

    cfg.fixtures = [
        FixtureInstance(type="par_rgb", dmx_start=1, x=100, y=100),
        FixtureInstance(type="blinder", dmx_start=65, x=400, y=400),
    ]
    stage.sync_from_config(cfg)

    _assert([i.type for i, _ in stage._render_groups] == ["par_rgb", "blinder"],
            "the stage now shows the new show's patch")
    _assert(all(obj.scene() is None for obj in before),
            "and every body of the old one has left the scene")
    _assert(len(stage.render_objs) == 2, "with no bodies left over")


def test_sync_keeps_unchanged_fixtures_identical(qapp):
    print("test_sync_keeps_unchanged_fixtures_identical")
    # Same reason add/remove are incremental: a body torn down and rebuilt is a
    # body the view may have been mid-paint on.
    stage, cfg = _stage()
    kept = cfg.fixtures[0]
    kept_bodies = [obj for inst, objs in stage._render_groups
                   if inst is kept for obj in objs]

    cfg.fixtures = [kept, FixtureInstance(type="par_rgb", dmx_start=70,
                                          x=200, y=200)]
    stage.sync_from_config(cfg)

    still = [obj for inst, objs in stage._render_groups
             if inst is kept for obj in objs]
    _assert(still == kept_bodies,
            "a fixture in both patches keeps the very same bodies")
    _assert(len(stage._render_groups) == 2, "and the new one was added beside it")


def test_one_resolve_feeds_every_body_of_a_fixture(qapp):
    print("test_one_resolve_feeds_every_body_of_a_fixture")
    from stage.controller import StageController
    cfg = Config()
    cfg.fixtures = [FixtureInstance(
        type="ledbar", dmx_start=1,
        bars=[FixturePosition(150 + i * 175, 350) for i in range(4)])]
    stage = StageController(cfg)

    data = bytearray(96)
    data[0:7] = bytes([255, 10, 20, 30, 0, 255, 0])
    stage.update_fixtures_with_dmx(data)

    bars = stage.led_bars
    _assert(len(bars) == 4, "the LED bar has several bodies")
    first = bars[0].state
    _assert(all(bar.state is first for bar in bars),
            "they share one resolved state — the same slice resolved once, "
            "not once per bar")
    _assert(first.rgb == (30, 20, 10), "and it is the right one")


def test_a_new_fixture_is_rigged_by_where_it_lands(qapp):
    print("test_a_new_fixture_is_rigged_by_where_it_lands")
    sc, _ = _stage()
    height = sc.scene.sceneRect().height()

    truss = sc.add_fixture("par_rgb", 9, 400, int(height * 0.2))
    deck = sc.add_fixture("par_rgb", 13, 500, int(height * 0.9))
    _assert(truss.hanging, "dropped up in the air, a fixture is flown")
    _assert(not deck.hanging, "dropped on the deck, it stands on it")

    # The browser drops at the stage centre, and what lands there is on its way
    # to a truss — so a guess of "standing" there would be wrong every time.
    middle = sc.add_fixture("par_rgb", 17, 600, int(height / 2))
    _assert(middle.hanging, "and the centre of the picture counts as flown")


def test_the_guess_is_only_a_guess_and_a_drag_never_revises_it(qapp):
    print("test_the_guess_is_only_a_guess_and_a_drag_never_revises_it")
    sc, _ = _stage()
    height = sc.scene.sceneRect().height()
    inst = sc.add_fixture("par_rgb", 9, 400, int(height * 0.2))
    body = sc.get_all_fixtures()[-1]

    # Dragging is a move, not a re-rig: a fixture that turned itself over
    # halfway across the stage would be unusable.
    body.setPos(400, height * 0.95)
    sc._on_fixture_moved(body)
    _assert(inst.hanging, "carried down to the deck, it is still flown")

    sc.set_fixture_hanging(inst, False)
    _assert(not inst.hanging, "until it is said outright")
    _assert(not sc.get_all_fixtures()[-1].hanging,
            "and the body is rebuilt the other way up")


def main():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    test_a_new_fixture_is_rigged_by_where_it_lands(app)
    test_the_guess_is_only_a_guess_and_a_drag_never_revises_it(app)
    test_add_is_incremental(app)
    test_added_fixture_receives_dmx(app)
    test_add_ledbar_seeds_all_bars(app)
    test_remove_drops_only_target(app)
    test_scene_uses_no_index(app)
    test_remove_ledbar_purges_bars(app)
    test_remove_then_readd_keeps_other_channels_stable(app)
    test_opening_another_show_replaces_the_fixtures(app)
    test_sync_keeps_unchanged_fixtures_identical(app)
    test_one_resolve_feeds_every_body_of_a_fixture(app)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
