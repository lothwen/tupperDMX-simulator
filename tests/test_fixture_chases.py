"""
Characterization test: the fixture browser's Chases tab and the sidecar writes
behind it.

The rules being pinned:

  * a step names a scene the way the runtime does — (bank name, index in that
    bank) — so the picker offers every scene of the type, bank first;
  * a chase read back carries the timing this editor shows even when the
    sidecar never named it, and writing puts back only what was actually
    authored: a chase nobody has timed round-trips unchanged;
  * the Save button carries ONLY the chase in the editor. Edits to the LIST
    (new, duplicate, delete, drag) are written as they happen, and edits to a
    chase are settled — saved or put back — before anything can move the editor
    off it;
  * a chase write touches `chases` and NOTHING else: the Scenes tab holds its
    own copy of the banks, unsaved edits and all, so the two tabs never write
    over each other;
  * `song_parts` are editable and survive an edit, because the runtime's
    song-part program is indexed off them — they are what lets auto mode reach
    a chase at all;
  * direction resolves to a play order, and bounce turns at the ends without
    repeating them;
  * Run drives the same ScenePreview the Scenes tab does, fading between steps
    and scaling each by its level.

Run with:
    uv run python tests/test_fixture_chases.py
"""

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import fixture_library
from core.fixture_library import clear_cache, load_fixture_defs, resolve_fixture

SCHEMA = ("https://raw.githubusercontent.com/OpenLightingProject/"
          "open-fixture-library/master/schemas/fixture.json")


def _doc(name):
    return {
        "$schema": SCHEMA,
        "name": name,
        "categories": ["Color Changer"],
        "meta": {"authors": ["test"], "createDate": "2026-01-01",
                 "lastModifyDate": "2026-01-02"},
        "availableChannels": {
            "brightness": {"capability": {"type": "Intensity"}},
            "red": {"capability": {"type": "ColorIntensity", "color": "Red"}},
            "green": {"capability": {"type": "ColorIntensity", "color": "Green"}},
            "blue": {"capability": {"type": "ColorIntensity", "color": "Blue"}},
        },
        "modes": [{"name": "4ch", "shortName": "4c",
                   "channels": ["brightness", "red", "green", "blue"]}],
    }


# Two banks, and two chases: one plain (no timing at all, the shape every chase
# written before this editor has) and one already timed.
SIDECAR = {
    "tdmx": 1,
    "notes": "hand-written; do not lose me",
    "modes": {
        "*": {
            "banks": {
                "FILLS": [
                    {"brightness": 255, "red": 255, "green": 0, "blue": 0,
                     "name": "Red wash"},
                    {"brightness": 128, "red": 0, "green": 0, "blue": 255,
                     "name": "Blue wash"},
                ],
                "STROBES": [
                    {"brightness": 255, "red": 255, "green": 255, "blue": 255,
                     "name": "White"},
                ],
            },
            "chases": [
                {"name": "Plain", "banks": ["FILLS", "STROBES"],
                 "scenes": [0, 0], "song_parts": ["CHORUS"]},
                {"name": "Timed", "banks": ["FILLS", "FILLS"], "scenes": [0, 1],
                 "fades": [2, 1.5], "holds": [2, 1], "levels": [100, 60],
                 "direction": "bounce", "units": "beats"},
            ],
        }
    },
}


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


@contextmanager
def _answers(button, log=None):
    """Answer every confirmation with `button`, recording what was asked.

    The tab asks before discarding or deleting, and an unanswered modal would
    hang the run — so `_with_tree` makes an unexpected one an assertion, and a
    test that expects one wraps it in here.
    """
    from PyQt6.QtWidgets import QMessageBox
    original = QMessageBox.question

    def stub(_parent, _title, text, *args, **kwargs):
        if log is not None:
            log.append(text)
        return getattr(QMessageBox.StandardButton, button)

    QMessageBox.question = staticmethod(stub)
    try:
        yield
    finally:
        QMessageBox.question = original


@contextmanager
def _timing(enabled=True):
    """Run with per-step chase timing on (or off).

    `chases_tab.CHASE_TIMING_ENABLED` gates the controls the runtime cannot
    play yet — direction, seconds/beats, and each step's fade/hold/level. It is
    read while the widgets are BUILT, so the tab has to be constructed inside
    this. Keeping the feature under test is the whole reason it is a flag and
    not a block of commented-out code.
    """
    from ui.fixture import chases_tab
    original = chases_tab.CHASE_TIMING_ENABLED
    chases_tab.CHASE_TIMING_ENABLED = enabled
    try:
        yield
    finally:
        chases_tab.CHASE_TIMING_ENABLED = original


def _with_tree(tree, body):
    """Run `body(shipped_dir)` with `tree` as the shipped fixtures."""
    from PyQt6.QtWidgets import QMessageBox
    orig_shipped = fixture_library.fixtures_dir_shipped
    orig_user = fixture_library.fixtures_dir_user
    orig_question = QMessageBox.question

    def unexpected(_parent, _title, text, *args, **kwargs):
        raise AssertionError(f"unexpected confirmation, which would hang: {text}")

    QMessageBox.question = staticmethod(unexpected)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            shipped, user = Path(tmp) / "shipped", Path(tmp) / "user"
            shipped.mkdir()
            user.mkdir()
            fixture_library.fixtures_dir_shipped = lambda: str(shipped)
            fixture_library.fixtures_dir_user = lambda: user
            clear_cache()
            for relative, document in tree.items():
                (shipped / relative).write_text(json.dumps(document))
            load_fixture_defs(force_reload=True)
            body(shipped)
    finally:
        QMessageBox.question = orig_question
        fixture_library.fixtures_dir_shipped = orig_shipped
        fixture_library.fixtures_dir_user = orig_user
        clear_cache()


TREE = {"wash-12.json": _doc("Wash 12"), "wash-12.tdmx.json": SIDECAR}


def _tab(type_name="Wash 12"):
    from ui.fixture.chases_tab import FixtureChasesTab
    tab = FixtureChasesTab()
    tab.set_fixture(type_name, resolve_fixture(type_name))
    return tab


def _written(shipped):
    return json.loads((shipped / "wash-12.tdmx.json").read_text())


# --- the model ----------------------------------------------------------------

def test_a_chase_reads_back_with_its_timing_spelled_out():
    print("test_a_chase_reads_back_with_its_timing_spelled_out")
    from ui.fixture.chases_tab import (
        BOUNCE, denormalise_chase, normalise_chase, run_order,
    )

    plain = normalise_chase(SIDECAR["modes"]["*"]["chases"][0])
    _assert(plain["fades"] == [0.0, 0.0] and plain["holds"] == [1.0, 1.0],
            "a chase with no timing keys reads back as a plain step-through")
    _assert(plain["levels"] == [100, 100] and plain["direction"] == "forward",
            "at full level, forwards")
    _assert(plain["song_parts"] == ["CHORUS"],
            "and keeps the auto-mode tags this editor never shows")
    _assert(denormalise_chase(plain) == SIDECAR["modes"]["*"]["chases"][0],
            "which is dropped again on the way out, so a chase nobody has "
            "timed round-trips byte for byte")

    timed = normalise_chase(SIDECAR["modes"]["*"]["chases"][1])
    _assert(timed["fades"] == [2.0, 1.5] and timed["levels"] == [100, 60],
            "an authored chase reads back what it was given")
    _assert(denormalise_chase(timed) == SIDECAR["modes"]["*"]["chases"][1],
            "and writes it back unchanged")

    ragged = normalise_chase({"banks": ["A", "B", "C"], "scenes": [0]})
    _assert(len(ragged["banks"]) == 1 and len(ragged["holds"]) == 1,
            "a step needs both halves, so a ragged pair truncates rather than "
            "inventing a scene index")

    three = dict(timed, banks=["A"] * 3, scenes=[0, 1, 2], direction=BOUNCE,
                 fades=[0] * 3, holds=[1] * 3, levels=[100] * 3)
    with _timing():
        _assert(run_order(timed) == [0, 1], "two steps bounce like they step")
        _assert(run_order(three) == [0, 1, 2, 1],
                "bounce turns at the ends without repeating them — playing an "
                "end twice reads as a stall, not a turn")
        _assert(run_order(dict(three, direction="backward")) == [2, 1, 0],
                "backward is the steps last to first")
    with _timing(False):
        _assert(run_order(three) == [0, 1, 2],
                "with per-step timing off every chase runs forwards, which is "
                "all update_chase does")


def test_level_dims_the_dimmer_and_falls_back_to_the_emitters():
    print("test_level_dims_the_dimmer_and_falls_back_to_the_emitters")
    from ui.fixture.chases_tab import level_channels

    def body(_shipped):
        fdef = resolve_fixture("Wash 12")
        _assert(level_channels(fdef) == [0],
                "LEVEL scales the fixture's Intensity channel, not everything")

    _with_tree(TREE, body)

    no_dimmer = _doc("No Dimmer")
    no_dimmer["modes"][0]["channels"] = ["red", "green", "blue"]

    def body2(_shipped):
        fdef = resolve_fixture("No Dimmer")
        _assert(level_channels(fdef) == [0, 1, 2],
                "a fixture with no dimmer is dimmed on its colour emitters — "
                "the same answer the engine gives when it resolves intensity")

    _with_tree({"no-dimmer.json": no_dimmer}, body2)


# --- the tab ------------------------------------------------------------------

def test_the_list_and_the_steps_come_off_the_definition(qapp):
    print("test_the_list_and_the_steps_come_off_the_definition")

    def body(_shipped):
        tab = _tab()
        _assert(tab.tree.topLevelItemCount() == 2, "one row per chase")
        _assert([tab.tree.topLevelItem(i).text(0) for i in range(2)]
                == ["Plain", "Timed"], "in the order the sidecar declares — "
                                       "note 70's velocity IS that index")
        _assert(tab.tree.topLevelItem(0).text(1) == "2×",
                "with its step count beside it")
        _assert(tab._selected is None and not tab.name_input.isEnabled(),
                "nothing starts selected: unlike a scene, a chase with no "
                "steps previews nothing, so there is no blank draft to edit")

        tab.tree.setCurrentItem(tab.tree.topLevelItem(1))
        _assert(tab.name_input.text() == "Timed",
                "picking a chase loads it into the editor")
        _assert(len(tab._rows) == 2, "one row per step")
        _assert(tab._rows[0].fade_field.value() == 2.0
                and tab._rows[1].level_slider.value() == 60,
                "carrying that step's fade, hold and level")
        _assert(tab._units_buttons["beats"].isChecked()
                and tab._direction_buttons["bounce"].isChecked(),
                "and the chase's units and direction")
        _assert(tab._rows[0].scene_combo.count() == 3,
                "the picker offers every scene the type has")
        _assert(tab._rows[0].scene_combo.itemText(0) == "FILLS · Red wash",
                "named bank-first, because a step stores (bank, index) — not "
                "a flat position")
        _assert(tab._rows[0].scene_combo.currentData() == ("FILLS", 0),
                "pointed at the scene the step names")
        _assert("2 steps" in tab.steps_summary.text()
                and "6.5b cycle" in tab.steps_summary.text(),
                "and the cycle is the steps' fades plus their holds")

        tab.tree.setCurrentItem(tab.tree.topLevelItem(0))
        _assert(tab._rows[0].hold_field.value() == 1.0,
                "an untimed chase edits at the defaults it read back as")
        _assert(not tab.save_button.isEnabled(),
                "and is NOT offered as unsaved work — merely looking at a "
                "chase is not editing it")

    with _timing():
        _with_tree(TREE, body)


def test_a_step_edit_rides_the_save_button(qapp):
    print("test_a_step_edit_rides_the_save_button")

    def body(shipped):
        tab = _tab()
        tab.tree.setCurrentItem(tab.tree.topLevelItem(0))
        tab._rows[0].fade_field._spin.setValue(2.5)
        _assert(tab.save_button.isEnabled(),
                "editing a step marks the chase unsaved")
        _assert(_written(shipped)["modes"]["*"]["chases"][0] ==
                SIDECAR["modes"]["*"]["chases"][0],
                "and nothing is on disk until the button is pressed")

        tab._rows[0].level_slider.setValue(40)
        tab._on_direction_picked("backward")
        tab.save_button.click()
        _assert(not tab.save_button.isEnabled(), "Save writes and goes quiet")

        written = _written(shipped)["modes"]["*"]["chases"][0]
        _assert(written["fades"] == [2.5, 0.0] and written["levels"] == [40, 100],
                "the edited step is on disk")
        _assert(written["direction"] == "backward", "and so is the direction")
        _assert("holds" not in written,
                "while the half nobody touched is still absent — an edit does "
                "not spray defaults across the file")
        _assert(written["song_parts"] == ["CHORUS"],
                "and the auto-mode tags this editor never shows survive it")

        _assert(_written(shipped)["notes"] == "hand-written; do not lose me",
                "the raw sidecar round-trips: unmodelled keys survive")

    with _timing():
        _with_tree(TREE, body)


def test_a_chase_write_leaves_the_banks_alone(qapp):
    print("test_a_chase_write_leaves_the_banks_alone")

    def body(shipped):
        tab = _tab()
        tab.tree.setCurrentItem(tab.tree.topLevelItem(0))
        tab._rows[0].level_slider.setValue(50)
        tab.save_button.click()
        _assert(_written(shipped)["modes"]["*"]["banks"]
                == SIDECAR["modes"]["*"]["banks"],
                "a chase write touches `chases` and nothing else — the Scenes "
                "tab is holding its own copy of the banks, unsaved edits and "
                "all, and a write carrying them would put that on disk")

    with _timing():
        _with_tree(TREE, body)


def test_the_list_writes_as_it_happens(qapp):
    print("test_the_list_writes_as_it_happens")

    def body(shipped):
        tab = _tab()
        writes = []
        tab.library_changed.connect(lambda: writes.append(True))

        tab.new_button.click()
        _assert(len(_written(shipped)["modes"]["*"]["chases"]) == 3
                and len(writes) == 1,
                "a new chase is on disk at the click — the list is what MIDI "
                "addresses, so it can't wait behind a button about something "
                "else")
        _assert(tab._selected == 2 and len(tab._rows) == 1,
                "selected, with a first step already naming a scene")

        tab.duplicate_button.click()
        _assert(len(_written(shipped)["modes"]["*"]["chases"]) == 4
                and len(writes) == 2, "so is a duplicate")
        _assert(_written(shipped)["modes"]["*"]["chases"][3]["name"]
                == "Chase 3 copy", "named for what it came from")

        with _answers("Yes"):
            tab.delete_button.click()
        _assert(len(_written(shipped)["modes"]["*"]["chases"]) == 3
                and len(writes) == 3, "and a delete")

        tab.tree.move_requested.emit(2, 0)
        _assert([c["name"] for c in _written(shipped)["modes"]["*"]["chases"]]
                == ["Chase 3", "Plain", "Timed"],
                "a drag reorders the chases and writes it — the order IS the "
                "note-70 velocity binding")
        _assert(len(writes) == 4, "announcing every one of those writes")

    _with_tree(TREE, body)


def test_leaving_an_edited_chase_settles_it(qapp):
    print("test_leaving_an_edited_chase_settles_it")

    def body(shipped):
        tab = _tab()
        tab.tree.setCurrentItem(tab.tree.topLevelItem(0))
        tab._rows[0].level_slider.setValue(10)
        asked = []
        with _answers("No", asked):
            tab.tree.setCurrentItem(tab.tree.topLevelItem(1))
        _assert(len(asked) == 1 and "Plain" in asked[0],
                "stepping onto another chase asks about the one being left")
        _assert(tab._chases[0]["levels"] == [100, 100],
                "and 'No' puts it back the way it was written")
        _assert("levels" not in _written(shipped)["modes"]["*"]["chases"][0],
                "with nothing written")

        tab.tree.setCurrentItem(tab.tree.topLevelItem(0))
        tab._rows[0].level_slider.setValue(10)
        with _answers("Yes"):
            tab.save_pending_changes()
        _assert(_written(shipped)["modes"]["*"]["chases"][0]["levels"]
                == [10, 100],
                "leaving the dialog with 'Yes' writes it — there is no "
                "autosave, so leaving quietly would drop the work")

    with _timing():
        _with_tree(TREE, body)


def test_steps_are_added_moved_and_removed(qapp):
    print("test_steps_are_added_moved_and_removed")

    def body(_shipped):
        tab = _tab()
        tab.tree.setCurrentItem(tab.tree.topLevelItem(1))
        tab.add_step_button.click()
        _assert(len(tab._rows) == 3, "a new step joins the end")
        _assert(tab._chases[1]["fades"][2] == tab._chases[1]["fades"][1],
                "copied off the last one, so a chase builds by tweaking")

        tab._rows[0].moved.emit(1)
        _assert(tab._chases[1]["scenes"][:2] == [1, 0],
                "▼ swaps a step with the one after it")
        tab._rows[2].removed.emit()
        _assert(len(tab._rows) == 2 and tab.tree.topLevelItem(1).text(1) == "2×",
                "✕ removes one, and the list's count follows")
        _assert(tab.save_button.isEnabled(),
                "all of which is chase CONTENT, so it rides the Save button "
                "rather than writing itself")

    _with_tree(TREE, body)


def test_run_drives_the_preview_through_the_steps(qapp):
    print("test_run_drives_the_preview_through_the_steps")

    def body(_shipped):
        tab = _tab()
        # The preview keeps no values of its own — it hands each frame straight
        # to the bodies — so record what it is fed.
        frames = []
        original = tab._preview.set_values
        tab._preview.set_values = lambda values: (frames.append(list(values)),
                                                  original(values))[1]

        tab.tree.setCurrentItem(tab.tree.topLevelItem(0))   # 2 steps, no fade
        _assert("Red wash" in tab.step_label.text() and frames[-1][1] == 255,
                "the still picture is the step the editor is looking at")
        _assert(tab._run_timer.isActive(),
                "and picking a chase runs it — a chase IS its motion, so a "
                "still frame of one step answers almost nothing about it")

        tab._run_elapsed = 5.0          # past the first step's 1s hold
        tab._on_run_tick()
        _assert("White" in tab.step_label.text() and frames[-1][2] == 255,
                "a tick past a step's hold advances to the next")

        tab.run_button.click()
        _assert(not tab._run_timer.isActive(),
                "the button is there for when you would rather it held still")
        tab.run_button.click()
        _assert(tab._run_timer.isActive(), "and starts it again")

        # Fades interpolate rather than snap: the second step of "Timed" fades
        # in over 1.5 beats, so partway through it is neither scene.
        tab.tree.setCurrentItem(tab.tree.topLevelItem(1))
        _assert(tab._run_timer.isActive(), "moving to another chase runs that one")
        tab._run_position, tab._run_elapsed = 1, 0.3
        tab._run_from = tab._step_values(tab._chases[1], 0)
        tab._on_run_tick()
        _assert(0 < frames[-1][3] < 255,
                "a step mid-fade is between where it came from and where it "
                "is going")
        _assert(tab._step_values(tab._chases[1], 1)[0] == 77,
                "and where it is going is that scene scaled by the step's "
                "LEVEL — a brightness of 128 authored at 60 %")

        # Hiding the tab — switching to Scenes, or closing the dialog — must
        # stop it: now that selecting runs, a chase left ticking at 30 Hz
        # against a preview nobody is looking at is the common case.
        tab.show()          # a hideEvent only follows an actual show
        tab.hide()
        _assert(not tab._run_timer.isActive(), "hiding the tab stops the run")
        tab._preview.set_values = original
        tab.preview.clear()

    with _timing():
        _with_tree(TREE, body)


def test_timing_off_hides_what_the_engine_cannot_play(qapp):
    print("test_timing_off_hides_what_the_engine_cannot_play")
    from ui.fixture.chases_tab import (
        ENGINE_FADE_SECONDS, ENGINE_STEP_SECONDS, denormalise_chase,
    )

    def body(shipped):
        with _timing(False):
            tab = _tab()
            tab.tree.setCurrentItem(tab.tree.topLevelItem(1))   # "Timed"
            _assert(not tab._direction_buttons and not tab._units_buttons,
                    "no direction and no units switch — the engine runs every "
                    "chase forwards on one interval")
            _assert(tab._rows[0].fade_field is None
                    and tab._rows[0].level_slider is None,
                    "and no per-step fade, hold or level")
            _assert(tab.steps_summary.text() == "2 steps",
                    "the summary drops a cycle length nothing would honour")
            _assert(tab._step_timing(tab._chases[1], 0)
                    == (ENGINE_FADE_SECONDS,
                        ENGINE_STEP_SECONDS - ENGINE_FADE_SECONDS),
                    "Run steps at the engine's own CHASE TIME and BLEND TIME, "
                    "so the preview shows the rig rather than a number "
                    "invented in the dialog")
            _assert(tab._step_values(tab._chases[1], 1)[0] == 128,
                    "and plays every step at full — the level of 60 % the "
                    "sidecar carries is not applied")

            # Still editable, and an untouched chase still round-trips.
            tab.name_input.setText("Renamed")
            tab._on_name_edited()
            tab.save_button.click()
            written = _written(shipped)["modes"]["*"]["chases"]
            _assert(written[1]["fades"] == [2, 1.5] and written[1]["levels"]
                    == [100, 60] and written[1]["direction"] == "bounce",
                    "a chase already carrying timing keeps every bit of it "
                    "through a save it cannot edit — the model still "
                    "round-trips what the UI is not showing")
            _assert(written[0] == SIDECAR["modes"]["*"]["chases"][0],
                    "and its untimed neighbour is untouched")

    _with_tree(TREE, body)


def test_song_part_tags_are_editable_and_reach_the_runtime(qapp):
    print("test_song_part_tags_are_editable_and_reach_the_runtime")
    from core.note_layout import song_part_refs

    def body(shipped):
        tab = _tab()
        tab.tree.setCurrentItem(tab.tree.topLevelItem(0))       # "Plain"
        _assert(tab._part_chips["CHORUS"].isChecked(),
                "a chase's existing song_parts show as checked tags")
        _assert(not tab._part_chips["INTRO"].isChecked(), "and only those")

        tab._part_chips["INST"].setChecked(True)
        tab._on_song_parts_changed()
        _assert(tab.save_button.isEnabled(),
                "tagging is chase content, so it rides the Save button")
        tab.save_button.click()
        written = _written(shipped)["modes"]["*"]["chases"][0]
        _assert(written["song_parts"] == ["CHORUS", "INST"],
                "and is written in SONG_PARTS order, the shape "
                "blinder.tdmx.json already uses")

        # The whole point of the tags: this is what auto mode indexes.
        clear_cache()
        program = song_part_refs(resolve_fixture("Wash 12"))
        _assert(any(ref.is_chase and ref.index == 0
                    for ref in program.get("INST", [])),
                "so the song-part program now offers the chase during INST — "
                "an untagged chase can only ever be fired by hand (note 70)")

        tab.tree.setCurrentItem(tab.tree.topLevelItem(1))
        _assert("No song parts" in tab.tree.topLevelItem(1).toolTip(0),
                "and the list says which chases auto mode cannot reach")

    _with_tree(TREE, body)


def test_the_dialog_mounts_the_tab_and_keeps_the_two_in_step(qapp):
    print("test_the_dialog_mounts_the_tab_and_keeps_the_two_in_step")
    from core.config import Config
    from ui.fixture.browser import AddFixtureDialog
    from stage.controller import StageController

    def body(_shipped):
        config = Config()
        config.fixtures = []
        dialog = AddFixtureDialog(StageController(config))
        _assert([dialog.tabs.tabText(i) for i in range(dialog.tabs.count())
                 if dialog.tabs.isTabVisible(i)]
                == ["General", "Channels", "Modes", "Scenes", "Chases"],
                "Chases joins the detail panel's tabs, beside Scenes")

        dialog.select_type("Wash 12")
        _assert([c.get("name") for c in dialog.chases_tab._chases]
                == ["Plain", "Timed"], "picking a type loads its chases")
        _assert(list(dialog.chases_tab._banks) == ["FILLS", "STROBES"],
                "and the banks its steps name")

        # The two tabs share a sidecar and hold their own copies of it. A bank
        # rename is the collision: the Scenes tab rewrites the chases naming
        # the bank, and the Chases tab would otherwise save the old name back.
        dialog.chases_tab.tree.setCurrentItem(
            dialog.chases_tab.tree.topLevelItem(0))
        dialog.scenes_tab._rename_bank("FILLS", "WASHES")
        _assert(dialog.chases_tab._chases[0]["banks"][0] == "WASHES",
                "so a rename on the Scenes tab reaches the Chases tab's copy")
        _assert("WASHES" in dialog.chases_tab._banks,
                "along with the bank the step picker offers")

        dialog.chases_tab._rows[0].level_slider.setValue(20)
        dialog.chases_tab.save_button.click()
        _assert([c.get("banks") for c in dialog.scenes_tab._chases]
                == [["WASHES", "STROBES"], ["WASHES", "WASHES"]],
                "and a chase write reaches the Scenes tab's copy, which is "
                "the one a later rename would follow through into")

    with _timing():
        _with_tree(TREE, body)


def main():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    test_a_chase_reads_back_with_its_timing_spelled_out()
    test_level_dims_the_dimmer_and_falls_back_to_the_emitters()
    test_the_list_and_the_steps_come_off_the_definition(app)
    test_a_step_edit_rides_the_save_button(app)
    test_a_chase_write_leaves_the_banks_alone(app)
    test_the_list_writes_as_it_happens(app)
    test_leaving_an_edited_chase_settles_it(app)
    test_steps_are_added_moved_and_removed(app)
    test_run_drives_the_preview_through_the_steps(app)
    test_timing_off_hides_what_the_engine_cannot_play(app)
    test_song_part_tags_are_editable_and_reach_the_runtime(app)
    test_the_dialog_mounts_the_tab_and_keeps_the_two_in_step(app)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
