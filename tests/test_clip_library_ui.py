"""
Characterization test: the clip library's two routes into the fixture editor.

Scenes and chases are edited per fixture TYPE, and the clip library is where
you meet them as cues — so it is where you ask to change one. Two gestures:

  * right-click a card -> "Edit <fixture>…", resolved from the clip's own ref,
    landing on the Chases tab for a chase and on Scenes for anything else;
  * the "+ New" tile, which authors a new scene and so opens the same editor —
    on the fixture the view is already about, and otherwise on the one the
    operator picks.

The rules being pinned:

  * a card that names no fixture (a global Show verb, a favourite whose fixture
    has been unpatched) offers nothing rather than opening the wrong thing;
  * the panel resolves the (type, mode) to an INSTANCE and names it in the
    menu, because a group is per type and a type can be patched twice;
  * what is asked for is the fixture's stable MIDI channel, never a row or a
    group index;
  * "+ New" falls through to a picker exactly when no single fixture owns the
    view: search mode, the Show group, the Favourites group.

The menu itself lives on the PANEL, not on the card: the card is destroyed by
the rebuild that a patch edit triggers, and it can name nothing but its own ref.

Offscreen Qt + a shared QApplication come from conftest.py (the `qapp` fixture).

Run with:
    uv run python tests/test_clip_library_ui.py
"""

import os
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.config import Config, shipped_fixtures


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _panel(fixtures=None):
    """A library panel over the shipped patch (ledbar ch0, blinders ch1/ch2)."""
    from ui.clip_library import ClipLibraryWidget
    config = Config()
    config.fixtures = shipped_fixtures() if fixtures is None else fixtures
    panel = ClipLibraryWidget(config)
    asked = []
    panel.edit_fixture_requested.connect(
        lambda ch, tab, ref: asked.append((ch, tab, ref)))
    return panel, config, asked


@contextmanager
def _menu_picks(index):
    """Answer the next QMenu.exec with its `index`-th action; None cancels.

    Yields the list of action labels each menu offered, so a test can assert
    what the operator was shown as well as what it did.
    """
    from PyQt6.QtWidgets import QMenu
    original = QMenu.exec
    offered = []

    def stub(self, *args, **kwargs):
        actions = self.actions()
        offered.append([a.text() for a in actions])
        if index is None or not (0 <= index < len(actions)):
            return None
        return actions[index]

    QMenu.exec = stub
    try:
        yield offered
    finally:
        QMenu.exec = original


def _group_index(panel, label):
    for i, group in enumerate(panel._groups):
        if group["label"] == label:
            return i
    raise AssertionError(f"no group '{label}' in the panel")


def _first_clip(panel, label):
    return panel._groups[_group_index(panel, label)]["clips"][0]


def _cards(panel):
    from ui.clip_library import ClipCard
    flow = panel.card_flow
    return [flow.itemAt(i).widget() for i in range(flow.count())
            if isinstance(flow.itemAt(i).widget(), ClipCard)]


# --- right-clicking a card ----------------------------------------------------

def test_a_card_menu_names_the_instance_and_opens_it(qapp):
    print("test_a_card_menu_names_the_instance_and_opens_it")
    panel, config, asked = _panel()
    clip = _first_clip(panel, "Fills")          # a ledbar scene

    with _menu_picks(0) as offered:
        panel._on_card_menu(clip["ref"], clip["type"])
    _assert(offered == [["Edit ledbar…"]],
            "the menu names the instance that will open, not the type in the "
            "abstract — a group is per type, and a type can be patched twice")
    _assert([(ch, tab) for ch, tab, _r in asked] == [(0, "scenes")],
            "and a scene card opens that fixture's Scenes tab, by MIDI channel")
    _assert(asked[0][2] == clip["ref"],
            "carrying the clip's own ref, so the editor can open ON that "
            "scene rather than merely on its tab")


def test_a_chase_card_lands_on_the_chases_tab(qapp):
    print("test_a_chase_card_lands_on_the_chases_tab")
    panel, _, asked = _panel()
    # The blinder's Chases group — the second one, after the ledbar's.
    index = [i for i, g in enumerate(panel._groups)
             if g["label"] == "Chases" and g.get("fixture") == "blinder"][0]
    clip = panel._groups[index]["clips"][0]

    with _menu_picks(0):
        panel._on_card_menu(clip["ref"], clip["type"])
    _assert([(ch, tab) for ch, tab, _r in asked] == [(1, "chases")],
            "a chase card opens the Chases tab of the blinder it belongs to")
    _assert(asked[0][2] == clip["ref"], "and names which chase")


def test_a_cancelled_card_menu_opens_nothing(qapp):
    print("test_a_cancelled_card_menu_opens_nothing")
    panel, _, asked = _panel()
    clip = _first_clip(panel, "Fills")

    with _menu_picks(None):
        panel._on_card_menu(clip["ref"], clip["type"])
    _assert(asked == [], "dismissing the menu asks for nothing")


def test_a_card_that_names_no_fixture_offers_nothing(qapp):
    """A global Show verb belongs to the show, not to a fixture; a favourite
    whose fixture has been unpatched resolves to nobody. Neither may open the
    editor on an arbitrary fixture."""
    print("test_a_card_that_names_no_fixture_offers_nothing")
    panel, _, asked = _panel()
    show_clip = _first_clip(panel, "Show")

    with _menu_picks(0) as offered:
        panel._on_card_menu(show_clip["ref"], show_clip["type"])
        panel._on_card_menu({}, "scene")                       # no ref at all
        panel._on_card_menu({"fixture": "gone-away"}, "scene")  # unpatched type
    _assert(offered == [], "no menu is even raised")
    _assert(asked == [], "and nothing is asked for")


def test_the_card_reports_a_right_press_not_a_release(qapp):
    """The card starts its drag from mouseMoveEvent and paints its own star
    target, both left-button-only — so the menu rides the right PRESS, where it
    cannot fight either."""
    print("test_the_card_reports_a_right_press_not_a_release")
    from PyQt6.QtCore import Qt
    panel, _, _ = _panel()
    panel._select_group(_group_index(panel, "Fills"))
    card = _cards(panel)[0]

    seen = []
    card.context_menu_requested.connect(lambda ref, t: seen.append((ref, t)))

    class _Event:
        def __init__(self, button):
            self._button = button
            self.accepted = False

        def button(self):
            return self._button

        def accept(self):
            self.accepted = True

    # The card is a live child of the panel, so the press also reaches the real
    # _on_card_menu — whose menu must be answered or the test blocks on it.
    event = _Event(Qt.MouseButton.RightButton)
    with _menu_picks(None):
        card.mousePressEvent(event)
    _assert(len(seen) == 1 and seen[0][0]["fixture"] == "ledbar",
            "a right press hands the panel the clip's own ref")
    _assert(seen[0][1] == "scene", "and its clip type")
    _assert(event.accepted, "and consumes the event")
    _assert(card._press_pos is None,
            "without arming the left-button drag the release would act on")


# --- the "+ New" tile ---------------------------------------------------------

def test_new_opens_the_fixture_the_view_is_about(qapp):
    print("test_new_opens_the_fixture_the_view_is_about")
    panel, _, asked = _panel()

    panel._select_group(_group_index(panel, "Blinder scenes"))
    panel._on_new_clicked()
    _assert(asked == [(1, "scenes", {})],
            "on a bank group, + New opens that bank's own fixture — no menu, "
            "because the view already says which fixture this is about, and "
            "no ref, because nothing in particular was asked for")


def test_new_falls_through_to_a_picker_when_no_fixture_owns_the_view(qapp):
    """Show, Favourites and search span more than one fixture (or none), so
    there is nothing to assume — ask instead."""
    print("test_new_falls_through_to_a_picker_when_no_fixture_owns_the_view")
    panel, _, asked = _panel()

    panel._select_group(_group_index(panel, "Show"))
    with _menu_picks(2) as offered:
        panel._on_new_clicked()
    _assert(len(offered) == 1 and len(offered[0]) == 3,
            "the Show group offers every patched fixture")
    _assert(offered[0][0].startswith("ledbar")
            and "ch 0" in offered[0][0],
            "each named with its MIDI channel, since two blinders share a type")
    _assert(asked == [(2, "scenes", {})], "and the pick is what opens")

    # Search spans every group, so the selected pill no longer says anything.
    asked.clear()
    panel._select_group(_group_index(panel, "Fills"))
    panel.search_box.setText("red")
    with _menu_picks(None) as offered:
        panel._on_new_clicked()
    _assert(len(offered) == 1, "in search mode + New asks as well")
    _assert(asked == [], "and a dismissed picker opens nothing")


def test_a_single_patched_fixture_skips_the_question(qapp):
    print("test_a_single_patched_fixture_skips_the_question")
    panel, config, asked = _panel(fixtures=shipped_fixtures()[:1])

    panel._select_group(_group_index(panel, "Show"))
    with _menu_picks(0) as offered:
        panel._on_new_clicked()
    _assert(offered == [], "with one fixture patched there is nothing to ask")
    _assert(asked == [(0, "scenes", {})], "it simply opens")


def test_new_on_an_empty_patch_does_nothing(qapp):
    print("test_new_on_an_empty_patch_does_nothing")
    panel, _, asked = _panel(fixtures=[])

    with _menu_picks(0) as offered:
        panel._on_new_clicked()
    _assert(offered == [] and asked == [],
            "the placeholder already says why; a menu of nothing would not help")


def test_the_new_tile_is_clickable(qapp):
    """It was drawn but inert. Its click reports index -1, which is not a group
    — the panel routes it to the editor rather than to a selection."""
    print("test_the_new_tile_is_clickable")
    from ui.clip_library import ClipPill
    panel, _, asked = _panel()
    pills = [panel.pill_flow.itemAt(i).widget()
             for i in range(panel.pill_flow.count())]
    new_tile = [p for p in pills if isinstance(p, ClipPill) and p._is_new]
    _assert(len(new_tile) == 1, "there is exactly one + New tile")

    before = panel._selected
    new_tile[0].clicked.emit(-1)
    _assert(panel._selected == before,
            "clicking it does not change the selected group")
    _assert(len(asked) == 1, "it opens the editor instead")


def main():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    test_a_card_menu_names_the_instance_and_opens_it(app)
    test_a_chase_card_lands_on_the_chases_tab(app)
    test_a_cancelled_card_menu_opens_nothing(app)
    test_a_card_that_names_no_fixture_offers_nothing(app)
    test_the_card_reports_a_right_press_not_a_release(app)
    test_new_opens_the_fixture_the_view_is_about(app)
    test_new_falls_through_to_a_picker_when_no_fixture_owns_the_view(app)
    test_a_single_patched_fixture_skips_the_question(app)
    test_new_on_an_empty_patch_does_nothing(app)
    test_the_new_tile_is_clickable(app)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
