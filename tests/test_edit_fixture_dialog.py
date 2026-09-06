"""
Characterization test: the fixture dialog's EDIT mode.

`AddFixtureDialog` answers three questions — which fixture, is this really the
one, and where does it go. For a fixture the show already has, the first is
already answered, so edit mode hides the library rail and points the same
header, tabs and footer at that instance. Everything else is the same dialog,
because it is the same set of answers.

The rules being pinned:

  * the library rail is gone and the type cannot be changed, but all five tabs
    and the whole patch footer are still there;
  * the footer measures the patch with the edited fixture EXCLUDED — against
    itself it would report overlapping itself the moment it opened;
  * nothing is written until OK: the name, rigging, address, channel and mode
    are answers held in widgets, which is what makes Cancel mean something;
  * scene banks are stored PER MODE, so the Modes tab re-points the library
    tabs — that part is shared with add mode and needs no edit-specific code;
  * Delete is not a patch decision waiting on OK: it confirms, removes by
    IDENTITY (there is no stage selection when this is opened from the timeline
    or the clip library) and closes;
  * closing settles unsaved scene work and clears both previews, in either mode.

Offscreen Qt + a shared QApplication come from conftest.py (the `qapp` fixture).

Run with:
    uv run python tests/test_edit_fixture_dialog.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from core.config import Config, FixtureInstance
from test_fixture_scenes import _answers, _assert, _with_tree, TWO_MODES


# A two-mode type whose modes carry DIFFERENT banks — which is what makes
# "changing the mode re-points the tabs" observable at all.
PER_MODE_SIDECAR = {
    "tdmx": 1,
    "modes": {
        "Basic": {
            "banks": {"BASICS": [
                {"red": 255, "green": 0, "blue": 0, "name": "Basic red"}]},
            "chases": [],
        },
        "Extended": {
            "banks": {"EXTRAS": [
                {"brightness": 255, "red": 0, "green": 255, "blue": 0,
                 "name": "Ext green"}]},
            "chases": [{"banks": ["EXTRAS"], "scenes": [0], "name": "C1"}],
        },
    },
}
TREE = {"wash-12.json": TWO_MODES, "wash-12.tdmx.json": PER_MODE_SIDECAR}


def _stage(*instances):
    from stage.controller import StageController
    config = Config()
    config.fixtures = list(instances)
    return StageController(config), config


def _fixture(mode="Extended", dmx_start=1, channel=0, name=""):
    return FixtureInstance(type="Wash 12", dmx_start=dmx_start, channel=channel,
                           mode=mode, name=name, x=300, y=300)


def _open(mode="Extended", dmx_start=1, channel=0, name="", extra=()):
    """An edit dialog over one patched 'Wash 12'."""
    from ui.fixture.browser import AddFixtureDialog
    instance = _fixture(mode, dmx_start, channel, name)
    stage, config = _stage(instance, *extra)
    return AddFixtureDialog(stage, instance=instance), instance, config, stage


# --- what edit mode is --------------------------------------------------------

def test_edit_mode_drops_the_library_rail_and_keeps_everything_else(qapp):
    print("test_edit_mode_drops_the_library_rail_and_keeps_everything_else")

    def body(_shipped, _user):
        dialog, instance, _cfg, _sc = _open(dmx_start=10, channel=2, name="Wash L")
        try:
            _assert(dialog._library_panel.isHidden(),
                    "the browser tree is hidden — a patched fixture cannot be "
                    "swapped for another type")
            tabs = [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
            _assert(tabs == ["General", "Channels", "Modes", "Scenes", "Chases"],
                    f"all five tabs are still here (got {tabs})")
            _assert(dialog.dmx_spin.value() == 10
                    and dialog.channel_spin.value() == 2,
                    "the footer opens on the fixture's own patch")
            _assert(dialog.detail_name.text() == "Wash L",
                    "the title is the fixture's own name")
            _assert(dialog.detail_model.text() == "Wash 12",
                    "with the model stepped back beside the manufacturer")
            _assert("Wash L" in dialog.windowTitle(),
                    "the window names the fixture, not the action")
            _assert(dialog.findChild(type(dialog.ok_button), "AddFxDelete")
                    is not None, "and the footer offers Delete")
        finally:
            dialog.reject()

    _with_tree(TREE, body)


def test_add_mode_is_untouched(qapp):
    print("test_add_mode_is_untouched")

    def body(_shipped, _user):
        from ui.fixture.browser import AddFixtureDialog
        from PyQt6.QtWidgets import QPushButton
        stage, _config = _stage()
        dialog = AddFixtureDialog(stage)
        try:
            _assert(not dialog._library_panel.isHidden(),
                    "adding still browses the library")
            _assert(dialog.windowTitle() == "Add fixture", "and says so")
            _assert(dialog.findChild(QPushButton, "AddFxDelete") is None,
                    "with nothing to delete")
            _assert(not hasattr(dialog, "name_edit"),
                    "and no instance fields, because there is no instance yet")
        finally:
            dialog.reject()

    _with_tree(TREE, body)


def test_the_footer_measures_the_patch_without_the_fixture_itself(qapp):
    """A fixture already occupies its own span and channel. Measured against
    itself it would collide the moment the dialog opened, and OK would be
    disabled on a patch nobody had touched."""
    print("test_the_footer_measures_the_patch_without_the_fixture_itself")

    def body(_shipped, _user):
        neighbour = _fixture(dmx_start=40, channel=5)
        dialog, _inst, _cfg, _sc = _open(dmx_start=10, channel=2,
                                         extra=(neighbour,))
        try:
            _assert(dialog.dmx_status_label.text() == "free · no overlap",
                    "its own address reads as free")
            _assert(dialog.ok_button.isEnabled(), "so OK is available")
            _assert(5 in dialog._used_channels and 2 not in dialog._used_channels,
                    "the neighbour's channel is taken, its own is not")

            dialog.dmx_spin.setValue(40)
            _assert(dialog.dmx_status_label.text().startswith("overlaps"),
                    "but the NEIGHBOUR is still measured, and reported")
            _assert(not dialog.ok_button.isEnabled(), "which disables OK")

            dialog.dmx_spin.setValue(10)
            dialog.channel_spin.setValue(5)
            _assert(dialog.channel_status_label.text() == "already used",
                    "and so is the neighbour's MIDI channel")
            _assert(not dialog.ok_button.isEnabled(), "which also disables OK")
        finally:
            dialog.reject()

    _with_tree(TREE, body)


def test_the_title_is_the_name_when_there_is_one_and_the_model_otherwise(qapp):
    """A rig has three Wash 12s, and calling all three "Wash 12" is exactly
    what naming them was for — so a named fixture leads with its name and the
    model steps back beside the manufacturer. Unnamed, the title IS the model
    and there is nothing to put beside the manufacturer."""
    print("test_the_title_is_the_name_when_there_is_one_and_the_model_otherwise")

    def body(_shipped, _user):
        named, _i, _c, _s = _open(name="Wash L")
        try:
            _assert(named.detail_name.text() == "Wash L", "named: the title is the name")
            _assert(named.detail_model.text() == "Wash 12"
                    and not named.detail_model.isHidden(),
                    "named: the model shows beside the manufacturer")
            _assert(named.detail_manufacturer.text() == "GENERIC",
                    "named: the manufacturer is still the first thing said")
        finally:
            named.reject()

        plain, _i, _c, _s = _open(name="")
        try:
            _assert(plain.detail_name.text() == "Wash 12",
                    "unnamed: the title is the model, as it always was")
            _assert(plain.detail_model.isHidden(),
                    "unnamed: and nothing is repeated beside the manufacturer")
        finally:
            plain.reject()

    _with_tree(TREE, body)


def test_the_title_renames_in_place(qapp):
    print("test_the_title_renames_in_place")

    def body(_shipped, _user):
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QToolButton
        dialog, instance, _cfg, _sc = _open(name="Wash L")
        try:
            _assert(dialog.name_edit.isHidden() and not dialog.detail_name.isHidden(),
                    "it opens showing the title, not an edit box")

            dialog.findChild(QToolButton, "AddFxRename").click()
            _assert(not dialog.name_edit.isHidden()
                    and dialog.detail_name.isHidden()
                    and dialog.rename_button.isHidden(),
                    "the pencil swaps the title for its editor, in place")
            _assert(dialog.name_edit.text() == "Wash L"
                    and dialog.name_edit.selectedText() == "Wash L",
                    "primed with the current name, selected ready to replace")

            dialog.name_edit.setText("Side fill")
            dialog.name_edit.editingFinished.emit()
            _assert(dialog.name_edit.isHidden() and not dialog.detail_name.isHidden(),
                    "committing puts the title back")
            _assert(dialog.detail_name.text() == "Side fill",
                    "carrying the new name")
            _assert(instance.name == "Wash L",
                    "which is still only an answer — OK is what applies it")

            # A double-click on the title is the other way in.
            class _E:
                def button(self):
                    return Qt.MouseButton.LeftButton

                def accept(self):
                    pass

            dialog.detail_name.mouseDoubleClickEvent(_E())
            _assert(not dialog.name_edit.isHidden(),
                    "a double-click on the title starts the rename too")

            # Escape belongs to the rename, not to the dialog: bubbling up it
            # would close the whole thing and lose every other edit.
            dialog.name_edit.setText("Nonsense")
            dialog.name_edit.cancelled.emit()
            _assert(dialog.name_edit.isHidden(), "escape ends the rename")
            _assert(dialog.detail_name.text() == "Wash L",
                    "putting back the name the fixture is still patched under")
            _assert(dialog.result() == 0, "and leaves the dialog open")

            dialog.accept()
            dialog.apply_edits()
            _assert(instance.name == "Wash L", "so OK applies the reverted name")
        finally:
            if dialog.result() == 0:
                dialog.reject()

    _with_tree(TREE, body)


# --- nothing is written until OK ----------------------------------------------

def test_edits_reach_the_instance_only_on_ok(qapp):
    print("test_edits_reach_the_instance_only_on_ok")

    def body(_shipped, _user):
        dialog, instance, _cfg, _sc = _open(mode="Basic", dmx_start=10, channel=2)
        dialog.name_edit.setText("Upstage left")
        dialog.dmx_spin.setValue(40)
        dialog.channel_spin.setValue(7)
        dialog.set_mode("Extended")
        dialog.hanging_switch.setChecked(not dialog.hanging_switch.isChecked())

        _assert(instance.name == "" and instance.dmx_start == 10
                and instance.channel == 2 and instance.mode == "Basic",
                "the instance is untouched while the dialog is open")

        dialog.accept()
        _assert(dialog.apply_edits() is True, "OK reports that something moved")
        _assert(instance.name == "Upstage left", "the name lands")
        _assert(instance.dmx_start == 40, "the address lands")
        _assert(instance.channel == 7, "the MIDI channel lands")
        _assert(instance.mode == "Extended", "and so does the mode")

    _with_tree(TREE, body)


def test_cancel_discards_every_patch_edit(qapp):
    print("test_cancel_discards_every_patch_edit")

    def body(_shipped, _user):
        dialog, instance, _cfg, _sc = _open(mode="Basic", dmx_start=10, channel=2)
        dialog.name_edit.setText("Nope")
        dialog.dmx_spin.setValue(40)
        dialog.set_mode("Extended")
        dialog.reject()

        _assert(instance.name == "" and instance.dmx_start == 10
                and instance.mode == "Basic",
                "a cancelled dialog leaves the fixture exactly as it was")

    _with_tree(TREE, body)


def test_applying_nothing_reports_nothing(qapp):
    """The host persists and rebuilds on the strength of this, so an OK that
    changed nothing must not claim otherwise."""
    print("test_applying_nothing_reports_nothing")

    def body(_shipped, _user):
        dialog, _inst, _cfg, _sc = _open(mode="Extended", dmx_start=10)
        dialog.accept()
        _assert(dialog.apply_edits() is False, "OK with no edits reports no change")

    _with_tree(TREE, body)


def test_rigging_is_offered_only_when_the_profile_can_hang(qapp):
    print("test_rigging_is_offered_only_when_the_profile_can_hang")

    def body(_shipped, _user):
        dialog, instance, _cfg, stage = _open(mode="Extended")
        try:
            _assert(dialog._can_rig is stage.can_rig_inverted(instance),
                    "the switch is offered on the profile's own answer")
            if dialog._can_rig:
                dialog.hanging_switch.setChecked(False)
                dialog.accept()
                dialog.apply_edits()
                _assert(instance.hanging is False,
                        "and turning it off stands the fixture on the deck")
        finally:
            if dialog.result() == 0:
                dialog.reject()

    _with_tree(TREE, body)


# --- the library half ---------------------------------------------------------

def test_the_modes_tab_repoints_both_library_tabs(qapp):
    """Scene banks are stored per mode, so the tabs' contents are wrong the
    moment the mode moves — the same rule add mode already relies on."""
    print("test_the_modes_tab_repoints_both_library_tabs")

    def body(_shipped, _user):
        dialog, _inst, _cfg, _sc = _open(mode="Basic")
        try:
            _assert(list(dialog.scenes_tab._banks) == ["BASICS"],
                    "it opens on the patched mode's banks")
            _assert(dialog.chases_tab._chases == [], "and that mode's chases")

            dialog.set_mode("Extended")
            _assert(list(dialog.scenes_tab._banks) == ["EXTRAS"],
                    "the Scenes tab follows the mode across")
            _assert(len(dialog.chases_tab._chases) == 1,
                    "as does the Chases tab")
            _assert(len(dialog.scenes_tab._faders) == 4,
                    "and the faders follow the new channel count")
        finally:
            dialog.reject()

    _with_tree(TREE, body)


def test_a_bank_rename_in_scenes_follows_through_into_chases(qapp):
    """A chase names its banks by NAME, so the Scenes tab rewrites the chases
    when a bank is renamed — and the Chases tab, holding its own copy, has to
    be handed the result or it will save the old names back over the top."""
    print("test_a_bank_rename_in_scenes_follows_through_into_chases")

    def body(_shipped, _user):
        dialog, _inst, _cfg, _sc = _open(mode="Extended")
        try:
            announced = []
            dialog.library_changed.connect(lambda: announced.append(True))
            _assert(dialog.chases_tab._chases[0]["banks"] == ["EXTRAS"],
                    "the chase names the bank it steps through")
            dialog.scenes_tab._rename_bank("EXTRAS", "HEADLINES")
            _assert(dialog.chases_tab._chases[0]["banks"] == ["HEADLINES"],
                    "and the Chases tab was handed the rewritten chases")
            _assert(announced, "the write is announced, so the host can rebuild")
        finally:
            dialog.reject()

    _with_tree(TREE, body)


def test_closing_settles_unsaved_work_and_clears_both_previews(qapp):
    print("test_closing_settles_unsaved_work_and_clears_both_previews")

    def body(shipped, _user):
        import json
        dialog, _inst, _cfg, _sc = _open(mode="Extended")
        dialog.select_tab("scenes")
        # Compose a scene on the faders without pressing Create: that is what
        # "unsaved" means here, and it must be asked about on the way out.
        dialog.scenes_tab._faders[1].slider.setValue(200)
        _assert(dialog.scenes_tab.preview._bodies,
                "the preview is live before the close")
        with _answers("Yes"):
            dialog.reject()

        written = json.loads((shipped / "wash-12.tdmx.json").read_text())
        banks = written["modes"]["Extended"]["banks"]
        _assert(sum(len(v) for v in banks.values()) == 2,
                "answering Yes on the way out writes the composed scene")
        _assert(dialog.scenes_tab.preview._bodies == []
                and dialog.chases_tab.preview._bodies == [],
                "and both previews are cleared — their bodies own timers on "
                "the shared 30 Hz stage clock")

    _with_tree(TREE, body)


def test_select_tab_opens_where_the_caller_asked(qapp):
    print("test_select_tab_opens_where_the_caller_asked")

    def body(_shipped, _user):
        dialog, _inst, _cfg, _sc = _open(mode="Extended")
        try:
            for name, expected in (("chases", "Chases"), ("scenes", "Scenes")):
                dialog.select_tab(name)
                _assert(dialog.tabs.tabText(dialog.tabs.currentIndex()) == expected,
                        f"'{name}' opens the {expected} tab — a chase card "
                        f"should not land you on the scene list")
            current = dialog.tabs.currentIndex()
            dialog.select_tab("nonsense")
            _assert(dialog.tabs.currentIndex() == current,
                    "and a tab that is not here leaves the choice alone")
        finally:
            dialog.reject()

    _with_tree(TREE, body)


def test_the_channel_overview_folds_when_the_footer_cannot_fit_it(qapp):
    """1280 is the supported minimum display, and an edit dialog carries a
    Delete button the add one does not — so its footer is the widest thing
    here. Squeezed rather than folded, the sixteen channel buttons render as a
    smear and clip the actions off the end of the row."""
    print("test_the_channel_overview_folds_when_the_footer_cannot_fit_it")

    def body(_shipped, _user):
        from ui.fixture.browser import AddFixtureDialog
        instance = _fixture()
        stage, _config = _stage(instance)
        for label, dialog in (("add", AddFixtureDialog(stage)),
                              ("edit", AddFixtureDialog(stage, instance=instance))):
            try:
                for width in (940, 1180, 1600):
                    dialog.resize(width, 780)
                    # Shown, because the thresholds are only true once the
                    # style has polished the row it measures.
                    dialog.show()
                    needed = dialog._measure_footer()
                    _assert(needed <= width,
                            f"{label} @{width}: the footer fits in the window "
                            f"(needs {needed})")
                _assert(dialog._footer_min_full > dialog._footer_min_folded,
                        f"{label}: folding the buttons is measured as a saving "
                        f"— equal thresholds would mean it never folds")
            finally:
                dialog.close()
                dialog.reject()

    _with_tree(TREE, body)


def test_the_library_lists_open_at_a_share_of_the_width_and_are_draggable(qapp):
    """How much room a scene list wants depends on how long the fixture
    author's bank and scene names are, which a fixed rail cannot know. So it
    opens at a share of the width — a wide dialog gives a wide list — with a
    floor under it and a divider for when the share is wrong."""
    print("test_the_library_lists_open_at_a_share_of_the_width_and_are_draggable")

    def body(_shipped, _user):
        from ui.fixture.scenes_tab import _LIST_MIN_WIDTH, _LIST_RATIO
        from ui.fixture import chases_tab as chases
        _assert((chases._LIST_RATIO, chases._LIST_MIN_WIDTH)
                == (_LIST_RATIO, _LIST_MIN_WIDTH),
                "both tabs use the same share and the same floor — they are "
                "one editor over different data, and two widths would read "
                "as an accident")

        dialog, _inst, _cfg, _sc = _open(mode="Extended")
        try:
            for width, label in ((1000, "narrow"), (1600, "wide")):
                dialog.resize(width, 780)
                dialog.show()
                for name, tab in (("scenes", dialog.scenes_tab),
                                  ("chases", dialog.chases_tab)):
                    # A tab page is only laid out once it is the current one,
                    # which is exactly when its share is first applied.
                    dialog.select_tab(name)
                    sizes = tab.splitter.sizes()
                    share = sizes[0] / max(1, sum(sizes))
                    _assert(abs(share - _LIST_RATIO) < 0.02,
                            f"{name} @{label}: the list opens at "
                            f"{round(share * 100)}% of the width")
                    _assert(sizes[0] >= _LIST_MIN_WIDTH,
                            f"{name} @{label}: and never below its floor")

            # A drag is the point of the divider, so it must survive the next
            # resize rather than snapping back to the default share.
            scenes = dialog.scenes_tab.splitter
            total = sum(scenes.sizes())
            scenes.setSizes([int(total * 0.5), total - int(total * 0.5)])
            dialog.resize(1900, 780)
            sizes = scenes.sizes()
            share = sizes[0] / max(1, sum(sizes))
            _assert(abs(share - 0.5) < 0.03,
                    f"a dragged divider keeps its proportion when the dialog "
                    f"is widened (got {round(share * 100)}%)")
            _assert(not scenes.childrenCollapsible(),
                    "and neither pane can be dragged out of existence — there "
                    "is no affordance to bring one back")
        finally:
            dialog.close()
            dialog.reject()

    _with_tree(TREE, body)


def test_a_clip_opens_the_editor_on_the_scene_or_chase_it_names(qapp):
    """Landing on the right tab is not the same as landing on the right scene.
    A card is about one cue, so the editor opens on that one."""
    print("test_a_clip_opens_the_editor_on_the_scene_or_chase_it_names")

    def body(_shipped, _user):
        from core import clips as clip_model, note_layout
        dialog, _inst, _cfg, _sc = _open(mode="Extended")
        try:
            dialog.reveal(clip_model.scene_ref("Wash 12", "Extended",
                                               "EXTRAS", "Ext green", 0))
            _assert(dialog.tabs.tabText(dialog.tabs.currentIndex()) == "Scenes",
                    "a scene ref opens the Scenes tab")
            item = dialog.scenes_tab.tree.currentItem()
            _assert(item is not None and item.text(0) == "Ext green",
                    "with the cursor on the scene the card named")
            _assert(dialog.scenes_tab.name_input.text() == "Ext green",
                    "and the editor loaded from it — the cursor really moved, "
                    "rather than being set behind blocked signals")

            dialog.reveal(clip_model.note_ref("Wash 12", "Extended",
                                              note_layout.CHASE_LOAD, 0))
            _assert(dialog.tabs.tabText(dialog.tabs.currentIndex()) == "Chases",
                    "a chase ref opens the Chases tab")
            item = dialog.chases_tab.tree.currentItem()
            _assert(item is not None and item.text(0) == "C1",
                    "on the chase its velocity indexes — note 70 loads a chase "
                    "by index and by nothing else")

            # A channel control names no scene, so the tab is as close as this
            # can get; it must not guess at one.
            dialog.select_tab("scenes")
            dialog.scenes_tab.tree.setCurrentItem(None)
            dialog.reveal(clip_model.note_ref("Wash 12", "Extended", 1, 200))
            _assert(dialog.scenes_tab.tree.currentItem() is None,
                    "a channel clip selects nothing rather than guessing")
            dialog.reveal({})
            dialog.reveal(None)
            _assert(True, "and an absent ref is simply nothing to reveal")
        finally:
            dialog.reject()

    _with_tree(TREE, body)


def test_a_scene_is_revealed_by_name_with_the_index_only_breaking_ties(qapp):
    """Names repeat, so the index breaks the tie — but a reordered bank must
    still land on the scene the card was about, not on whatever now sits at
    its old row."""
    print("test_a_scene_is_revealed_by_name_with_the_index_only_breaking_ties")

    def body(_shipped, _user):
        dialog, _inst, _cfg, _sc = _open(mode="Extended")
        try:
            tab = dialog.scenes_tab
            tab._banks["EXTRAS"] = [
                {"name": "Twin", "brightness": 10},
                {"name": "Only me", "brightness": 20},
                {"name": "Twin", "brightness": 30},
            ]
            tab._rebuild_tree()

            _assert(tab.select_scene("EXTRAS", "Only me", 0),
                    "a name that moved is still found")
            _assert(tab.tree.currentItem().text(0) == "Only me",
                    "at its new row, not at the index the card remembered")

            _assert(tab.select_scene("EXTRAS", "Twin", 2),
                    "a repeated name uses the index to choose between them")
            _assert(tab.tree.currentItem().text(0) == "Twin"
                    and tab._selected == ("EXTRAS", 2),
                    "landing on the one the index names")

            _assert(tab.select_scene("EXTRAS", "Renamed since", 1),
                    "a scene renamed since the card was built falls back to "
                    "the index — better the right row than nothing")
            _assert(tab._selected == ("EXTRAS", 1), "which is where it lands")

            _assert(not tab.select_scene("NO SUCH BANK", "Twin", 0),
                    "a bank that is gone reveals nothing")
        finally:
            dialog.reject()

    _with_tree(TREE, body)


# --- delete -------------------------------------------------------------------

def test_delete_removes_the_instance_with_nothing_selected(qapp):
    """The stage has no selection here — the dialog was opened from the
    timeline, not by clicking the fixture. Delete must still find it."""
    print("test_delete_removes_the_instance_with_nothing_selected")

    def body(_shipped, _user):
        from PyQt6.QtWidgets import QMessageBox, QPushButton
        dialog, instance, config, stage = _open(mode="Extended")
        _assert(len(stage.scene.selectedItems()) == 0, "nothing is selected")

        original = QMessageBox.question
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Yes)
        try:
            dialog.findChild(QPushButton, "AddFxDelete").click()
        finally:
            QMessageBox.question = original

        _assert(instance not in config.fixtures, "the fixture leaves the patch")
        _assert(dialog.result() == 1, "and the dialog accepts itself closed")
        _assert(dialog.was_deleted(), "flagged as a delete, not as an OK")
        _assert(dialog.apply_edits() is False,
                "so the host applies nothing to a fixture that is gone")

    _with_tree(TREE, body)


def test_a_cancelled_delete_keeps_the_fixture_and_the_dialog(qapp):
    print("test_a_cancelled_delete_keeps_the_fixture_and_the_dialog")

    def body(_shipped, _user):
        from PyQt6.QtWidgets import QMessageBox, QPushButton
        dialog, instance, config, _sc = _open(mode="Extended")
        original = QMessageBox.question
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.No)
        try:
            dialog.findChild(QPushButton, "AddFxDelete").click()
        finally:
            QMessageBox.question = original

        _assert(instance in config.fixtures, "the fixture is still patched")
        _assert(not dialog.was_deleted(), "and nothing was flagged")
        dialog.reject()

    _with_tree(TREE, body)


def main():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    test_edit_mode_drops_the_library_rail_and_keeps_everything_else(app)
    test_add_mode_is_untouched(app)
    test_the_footer_measures_the_patch_without_the_fixture_itself(app)
    test_the_title_is_the_name_when_there_is_one_and_the_model_otherwise(app)
    test_the_title_renames_in_place(app)
    test_edits_reach_the_instance_only_on_ok(app)
    test_cancel_discards_every_patch_edit(app)
    test_applying_nothing_reports_nothing(app)
    test_rigging_is_offered_only_when_the_profile_can_hang(app)
    test_the_modes_tab_repoints_both_library_tabs(app)
    test_a_bank_rename_in_scenes_follows_through_into_chases(app)
    test_closing_settles_unsaved_work_and_clears_both_previews(app)
    test_select_tab_opens_where_the_caller_asked(app)
    test_a_clip_opens_the_editor_on_the_scene_or_chase_it_names(app)
    test_a_scene_is_revealed_by_name_with_the_index_only_breaking_ties(app)
    test_the_channel_overview_folds_when_the_footer_cannot_fit_it(app)
    test_the_library_lists_open_at_a_share_of_the_width_and_are_draggable(app)
    test_delete_removes_the_instance_with_nothing_selected(app)
    test_a_cancelled_delete_keeps_the_fixture_and_the_dialog(app)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
