"""
Characterization test: the generalized clip-edit dialog.

The rules being pinned:

  * a clip is edited against ITS OWN FIXTURE — a blinder scene opens with the
    blinder's eight channels, not the LED bar's seven, which is the whole
    reason this dialog replaced the two that came before it;
  * only the channels a scene CARRIES, plus the ones the user moved, are
    submitted: at runtime an absent channel keeps whatever the last frame left
    in it, while a present one at its default forces that value, so widening a
    scene silently would change the look;
  * the verb picker offers exactly what `note_applies_to` would let you DROP —
    no more (nothing the fixture can't do) and no less;
  * a fixture wider than 23 slots has its tail greyed out rather than silently
    misfiring, because notes 24+ are the scene-load verbs;
  * retyping across verb classes RESETS the velocity — the same byte means a
    channel value, a chase index or tenths of a second depending on the verb;
  * closing the dialog drops the preview bodies, which own animation timers on
    the shared 30 Hz clock.

Run with:
    uv run python tests/test_clip_edit_dialog.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import fixture_library, note_layout
from core.fixture_library import clear_cache, load_fixture_defs, resolve_fixture
from midi.constants import _channel_to_velocity
from tests import ofl_factory
from timeline.widgets.clip_edit import ADDRESSABLE_CHANNELS, ClipEditDialog
from timeline.widgets.verb_editors import (
    ChannelValueEditor, ChaseEditor, NoParamEditor, PackedSceneEditor,
    SceneLoadEditor, TimeEditor, default_velocity_for, editor_class_for,
)

BLINDER_SCENE = {"brightness": 200, "cool1": 180, "cool2": 160, "warm1": 140,
                 "warm2": 120, "strobe": 100, "mode": 80, "speed": 60}


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _blinder_scene_dialog(channels=None, present=None):
    fdef = resolve_fixture("blinder")
    channels = BLINDER_SCENE if channels is None else channels
    present = set(channels) if present is None else present
    return ClipEditDialog.for_scene(
        fdef=fdef, type_name="blinder", channel=1, dmx_start=33,
        name="B", channels=channels, present=present, time=4.0)


def _wide_fdef(count):
    channels = {f"c{i:02d}": ofl_factory.channel("Intensity") for i in range(count)}
    return ofl_factory.fdef(name=f"wide{count}", channels=channels,
                            mode_channels=list(channels))


# ---------------------------------------------------------------------------
# The regression this whole change exists for.
# ---------------------------------------------------------------------------
def test_a_blinder_scene_opens_as_a_blinder(qapp):
    print("test_a_blinder_scene_opens_as_a_blinder")
    dialog = _blinder_scene_dialog()
    try:
        _assert(len(dialog.scene_faders) == 8,
                f"one fader per blinder channel ({len(dialog.scene_faders)})")
        names = [f.name_label.text() for f in dialog.scene_faders]
        _assert(names[1].lower().startswith("cold")
                or "cool" in names[1].lower(),
                f"named from the blinder's own channels ({names})")
        values = dialog.scene_faders.values()
        _assert(values == [200, 180, 160, 140, 120, 100, 80, 60],
                f"every channel loads its own value ({values})")

        result = dialog.get_values()
        _assert(result.kind == "scene", "reports a scene edit")
        _assert(result.channels == BLINDER_SCENE,
                f"and returns all 8 fields ({result.channels})")
    finally:
        dialog.close()


def test_only_carried_and_touched_channels_are_submitted(qapp):
    print("test_only_carried_and_touched_channels_are_submitted")
    partial = {"brightness": 200, "cool1": 180}
    dialog = _blinder_scene_dialog(channels=partial)
    try:
        _assert(len(dialog.scene_faders) == 8,
                "the whole fixture is shown, not just the carried channels")
        _assert(dialog.get_values().channels == partial,
                "but an untouched dialog submits only what the scene carried")

        dialog.scene_faders[3].slider.setValue(140)      # warm1
        submitted = dialog.get_values().channels
        _assert(set(submitted) == {"brightness", "cool1", "warm1"},
                f"moving a dimmed fader adds exactly that channel ({submitted})")
        _assert(submitted["warm1"] == 140, "carrying the value moved to")
    finally:
        dialog.close()


# ---------------------------------------------------------------------------
# The verb picker.
# ---------------------------------------------------------------------------
def _offered_notes(dialog):
    return [dialog.verb_combo.itemData(i)
            for i in range(dialog.verb_combo.count())
            if dialog.verb_combo.itemData(i) is not None
            and dialog.verb_combo.itemData(i) >= 0]


def test_the_picker_offers_only_droppable_verbs(qapp):
    print("test_the_picker_offers_only_droppable_verbs")
    fdef = resolve_fixture("ledbar")
    dialog = ClipEditDialog.for_clip(
        fdef=fdef, type_name="ledbar", channel=0, dmx_start=1,
        note=4, velocity=100, time=3.0)
    try:
        notes = _offered_notes(dialog)
        _assert(all(note_layout.note_applies_to(n, fdef) for n in notes),
                "every offered verb would also be accepted as a drop")

        channels = [n for n in notes
                    if note_layout.CHANNEL_OFFSET_RANGE[0] <= n
                    <= note_layout.CHANNEL_OFFSET_RANGE[1]]
        _assert(channels == list(range(1, 8)),
                f"the ledbar's 7 channels and no more ({channels})")
        _assert(not any(note_layout.is_song_part(n) for n in notes),
                "song parts stay off a fixture lane — they carry no channel")
        _assert(dialog.current_note() == 4, "opens on the clip's own verb")
    finally:
        dialog.close()


def test_a_fixture_without_banks_is_offered_no_scene_verbs(qapp):
    print("test_a_fixture_without_banks_is_offered_no_scene_verbs")
    fdef = _wide_fdef(4)                       # synthetic: no banks, no chases
    dialog = ClipEditDialog.for_clip(
        fdef=fdef, type_name="wide4", channel=0, dmx_start=1,
        note=1, velocity=50, time=0.0)
    try:
        notes = _offered_notes(dialog)
        scene_loads = [n for n in notes
                       if note_layout.SCENE_LOAD_RANGE[0] <= n
                       <= note_layout.SCENE_LOAD_RANGE[1]]
        banks = [n for n in notes
                 if note_layout.BANK_SELECT_RANGE[0] <= n
                 <= note_layout.BANK_SELECT_RANGE[1]]
        _assert(not scene_loads, f"no scene verbs ({scene_loads})")
        _assert(not banks, f"no bank verbs ({banks})")
        _assert(note_layout.CHASE_LOAD not in notes, "and no chase verb")
    finally:
        dialog.close()


def test_channels_past_the_addressable_limit_are_greyed(qapp):
    print("test_channels_past_the_addressable_limit_are_greyed")
    fdef = _wide_fdef(30)
    dialog = ClipEditDialog.for_scene(
        fdef=fdef, type_name="wide30", channel=0, dmx_start=1, name="W",
        channels={"c00": 100}, present={"c00"}, time=0.0)
    try:
        _assert(len(dialog.scene_faders) == 30, "all 30 slots are shown")
        reachable = [f.index for f in dialog.scene_faders if f.isEnabled()]
        _assert(reachable == list(range(ADDRESSABLE_CHANNELS)),
                f"only the first {ADDRESSABLE_CHANNELS} are live "
                f"({len(reachable)})")
        _assert("cannot be driven" in dialog.status_label.text(),
                f"and the status line says why ({dialog.status_label.text()!r})")

        # Even a fader moved past the cap must not reach the result: there is
        # no MIDI note that would carry it.
        dialog.scene_faders[25].slider.setValue(200)
        _assert("c25" not in dialog.get_values().channels,
                "an unaddressable channel never reaches the write")
    finally:
        dialog.close()


# ---------------------------------------------------------------------------
# Velocity: one byte, a different meaning per verb class.
# ---------------------------------------------------------------------------
def test_each_verb_class_gets_its_own_editor(qapp):
    print("test_each_verb_class_gets_its_own_editor")
    for note, expected in ((1, ChannelValueEditor), (23, ChannelValueEditor),
                           (24, SceneLoadEditor), (35, SceneLoadEditor),
                           (57, SceneLoadEditor), (58, SceneLoadEditor),
                           (59, PackedSceneEditor),
                           (52, TimeEditor), (53, TimeEditor), (54, TimeEditor),
                           (70, ChaseEditor),
                           (36, NoParamEditor), (48, NoParamEditor),
                           (50, NoParamEditor), (51, NoParamEditor),
                           (60, NoParamEditor), (72, NoParamEditor)):
        got = editor_class_for(note)
        _assert(got is expected,
                f"note {note} -> {expected.__name__} (got {got.__name__})")


def test_velocity_encodings_round_trip(qapp):
    print("test_velocity_encodings_round_trip")
    fdef = resolve_fixture("ledbar")

    def _dialog(note, velocity):
        return ClipEditDialog.for_clip(
            fdef=fdef, type_name="ledbar", channel=0, dmx_start=1,
            note=note, velocity=velocity, time=1.0)

    # channel value: velocity * 2 out, // 2 back
    dialog = _dialog(4, 100)
    try:
        editor = dialog.param_stack.currentWidget()
        _assert(editor.value() == 200, f"velocity 100 shows as 200 ({editor.value()})")
        editor._fader.slider.setValue(120)
        _assert(dialog.get_values().velocity == 60, "200 -> 120 -> velocity 60")
    finally:
        dialog.close()

    # blend time: tenths of a second
    dialog = _dialog(52, 25)
    try:
        editor = dialog.param_stack.currentWidget()
        _assert(abs(editor._spin.value() - 2.5) < 1e-6,
                f"velocity 25 shows as 2.5 s ({editor._spin.value()})")
        editor._spin.setValue(7.0)
        _assert(dialog.get_values().velocity == 70, "7.0 s -> velocity 70")
    finally:
        dialog.close()

    # packed scene: bank * 8 + scene
    dialog = _dialog(59, 29)
    try:
        editor = dialog.param_stack.currentWidget()
        _assert(editor._bank.currentIndex() == 3
                and editor._scene.currentIndex() == 5,
                "velocity 29 unpacks to bank 3, scene 5")
        editor._bank.setCurrentIndex(1)
        editor._scene.setCurrentIndex(2)
        _assert(dialog.get_values().velocity == 10, "bank 1, scene 2 -> 10")
    finally:
        dialog.close()

    # chase: the velocity IS the index
    dialog = _dialog(70, 3)
    try:
        editor = dialog.param_stack.currentWidget()
        _assert(editor._combo.currentIndex() == 3, "velocity 3 selects chase 3")
        editor._combo.setCurrentIndex(6)
        _assert(dialog.get_values().velocity == 6, "chase 6 -> velocity 6")
    finally:
        dialog.close()


def test_retyping_resets_the_velocity(qapp):
    print("test_retyping_resets_the_velocity")
    fdef = resolve_fixture("ledbar")
    dialog = ClipEditDialog.for_clip(
        fdef=fdef, type_name="ledbar", channel=0, dmx_start=1,
        note=4, velocity=100, time=1.0)      # channel Red at DMX 200
    try:
        _assert(dialog.get_values().velocity == 100, "starts at velocity 100")
        for index in range(dialog.verb_combo.count()):
            if dialog.verb_combo.itemData(index) == note_layout.CHASE_LOAD:
                dialog.verb_combo.setCurrentIndex(index)
                break
        result = dialog.get_values()
        _assert(result.note == note_layout.CHASE_LOAD, "retyped to the chase verb")
        _assert(result.velocity == 0,
                f"velocity reset, NOT carried as chase #100 ({result.velocity})")
        _assert(default_velocity_for(note_layout.CHASE_LOAD) == 0,
                "which is the chase verb's declared default")
    finally:
        dialog.close()


# ---------------------------------------------------------------------------
# The shell.
# ---------------------------------------------------------------------------
def test_a_new_clip_needs_a_verb_before_ok(qapp):
    print("test_a_new_clip_needs_a_verb_before_ok")
    from PyQt6.QtWidgets import QDialogButtonBox
    fdef = resolve_fixture("ledbar")
    dialog = ClipEditDialog.for_clip(
        fdef=fdef, type_name="ledbar", channel=0, dmx_start=1,
        note=None, velocity=0, time=2.0, is_new=True)
    try:
        ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
        _assert(not ok.isEnabled(), "OK is disabled until a verb is picked")
        _assert(dialog.get_values() is None, "and there is no result to take")

        for index in range(dialog.verb_combo.count()):
            if dialog.verb_combo.itemData(index) == 1:
                dialog.verb_combo.setCurrentIndex(index)
                break
        _assert(ok.isEnabled(), "picking one enables it")
        _assert(dialog.get_values().note == 1, "and the result is that verb")
    finally:
        dialog.close()


def test_the_add_path_can_start_a_scene(qapp):
    print("test_the_add_path_can_start_a_scene")
    fdef = resolve_fixture("ledbar")
    dialog = ClipEditDialog.for_clip(
        fdef=fdef, type_name="ledbar", channel=0, dmx_start=1,
        note=None, velocity=0, time=2.0, is_new=True)
    try:
        for index in range(dialog.verb_combo.count()):
            if dialog.verb_combo.itemData(index) == -2:      # _NEW_SCENE
                dialog.verb_combo.setCurrentIndex(index)
                break
        else:
            raise AssertionError("the add path offers no 'New scene' entry")

        _assert(dialog.body.currentIndex() == 0, "switches to the scene page")
        _assert(dialog.get_values().kind == "new_scene",
                "and reports a scene creation")
        _assert(dialog.get_values().channels == {},
                "with nothing carried until a fader is moved")

        dialog.scene_faders[0].slider.setValue(255)
        dialog.scene_faders[3].slider.setValue(255)
        result = dialog.get_values()
        _assert(set(result.channels) == {"brightness", "red"},
                f"only the channels dialled in ({result.channels})")
    finally:
        dialog.close()


def test_a_locked_lane_is_read_only(qapp):
    print("test_a_locked_lane_is_read_only")
    from PyQt6.QtWidgets import QDialogButtonBox
    dialog = ClipEditDialog.for_scene(
        fdef=resolve_fixture("blinder"), type_name="blinder", channel=1,
        dmx_start=33, name="B", channels=BLINDER_SCENE,
        present=set(BLINDER_SCENE), time=4.0, locked=True)
    try:
        ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
        _assert(not ok.isEnabled(), "OK is disabled on a locked lane")
        _assert(not dialog.scene_faders.isEnabled(), "the faders are dead")
        _assert("locked" in dialog.status_label.text().lower(),
                "and the status line says so")
    finally:
        dialog.close()


def test_closing_drops_the_preview_bodies(qapp):
    print("test_closing_drops_the_preview_bodies")
    dialog = _blinder_scene_dialog()
    _assert(dialog.scene_output.preview._bodies,
            "the scene page previews through the real stage engine")
    dialog.close()
    _assert(dialog.scene_output.preview._bodies == [],
            "and closing stops every body — they own 30 Hz timers")
    _assert(dialog.clip_output.preview._bodies == [],
            "the clip page's preview too")


def test_open_and_close_repeatedly(qapp):
    """The leak in test_closing_drops_the_preview_bodies is what turns into a
    SIGSEGV, so exercise it under repetition."""
    print("test_open_and_close_repeatedly")
    for _ in range(50):
        dialog = _blinder_scene_dialog()
        dialog.scene_faders[2].slider.setValue(90)
        dialog.get_values()
        dialog.close()
    _assert(True, "50 open/edit/close cycles without a crash")


# ---------------------------------------------------------------------------
# Channel affordances — chosen from capabilities, never from a fixture's name.
# ---------------------------------------------------------------------------
def test_the_capability_grid_is_data_driven(qapp):
    """The LED bar's animation grid used to be a directory of GIFs with
    `value = index * 4` baked in. It is now one tile per capability, valued
    from the capability's own dmxRange — the GIFs are just artwork."""
    print("test_the_capability_grid_is_data_driven")
    from ui.fixture.affordances import (
        CapabilityGrid, affordance_for, capability_choices, grid_applies)

    led = resolve_fixture("ledbar")
    kinds = [affordance_for(led, i) for i in range(led.channel_count)]
    _assert(kinds == ["fader", "fader", "fader", "fader", "grid",
                      "fader", "fader"],
            f"only the animation channel is a menu ({kinds})")

    choices = capability_choices(led.channels[4])
    _assert(len(choices) == 35, f"one per Effect capability ({len(choices)})")
    _assert(choices[0] == (0, "On") and choices[3] == (12, "Loading corta arriba"),
            f"named and valued by the definition ({choices[:4]})")
    _assert([v for v, _l in choices] == list(range(0, 35 * 4, 4)),
            "which reproduces the old index*4 mapping exactly")

    grid = CapabilityGrid()
    grid.set_channel(led.channels[4])
    _assert(len(grid) == 35, f"a tile per choice ({len(grid)})")
    _assert(sum(1 for t in grid._tiles if t._movie is not None) == 35,
            "each matched to its shipped GIF by the capability's own name")
    grid.set_value(12)
    _assert(grid.value() == 12, "and selecting round-trips the DMX value")

    blinder = resolve_fixture("blinder")
    _assert(all(affordance_for(blinder, i) == "fader"
                for i in range(blinder.channel_count)),
            "a blinder gets no grid — and so never shows the ledbar's GIFs")


def test_the_picker_is_discoverable_and_opens_on_a_real_click(qapp):
    """A picker reachable only by clicking an unmarked label may as well not
    exist, so the affordance is part of the feature: the ▾, the hand cursor,
    the tooltip and the card's hint. Driven by a REAL mouse click, not by
    calling the handler — that is the gap that hid this the first time."""
    print("test_the_picker_is_discoverable_and_opens_on_a_real_click")
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest

    led = resolve_fixture("ledbar")
    dialog = ClipEditDialog.for_scene(
        fdef=led, type_name="ledbar", channel=0, dmx_start=1, name="X",
        channels={c.field: 0 for c in led.channels},
        present={c.field for c in led.channels}, time=1.0)
    try:
        dialog.show()
        animation = dialog.scene_faders.fader(4).name_label
        plain = dialog.scene_faders.fader(1).name_label

        _assert(animation.text().endswith("▾"),
                f"the discrete channel is marked ({animation.text()!r})")
        _assert(animation.cursor().shape() == Qt.CursorShape.PointingHandCursor,
                "and takes a hand cursor")
        _assert("Click to choose" in animation.toolTip(),
                f"with a tooltip saying what a click does ({animation.toolTip()!r})")
        _assert(animation.property("pickable") == "true",
                "and a property the stylesheet underlines")

        _assert(not plain.text().endswith("▾")
                and plain.cursor().shape() != Qt.CursorShape.PointingHandCursor,
                f"a continuous channel stays a caption ({plain.text()!r})")
        _assert("picker" in dialog.scene_faders.hint_label.text(),
                f"the card's hint mentions it too "
                f"({dialog.scene_faders.hint_label.text()!r})")

        QTest.mouseClick(plain, Qt.MouseButton.LeftButton)
        popovers = [w for w in qapp.topLevelWidgets()
                    if w.objectName() == "ClipEditPopover" and w.isVisible()]
        _assert(not popovers, "clicking a plain channel's name does nothing")

        QTest.mouseClick(animation, Qt.MouseButton.LeftButton)
        popovers = [w for w in qapp.topLevelWidgets()
                    if w.objectName() == "ClipEditPopover" and w.isVisible()]
        _assert(len(popovers) == 1,
                f"a real click on the marked name opens the picker ({len(popovers)})")
        grid = popovers[0].grid
        _assert(len(grid) == 35, f"showing all 35 choices at once ({len(grid)})")
        _assert(all(t._movie is not None for t in grid._tiles),
                "each one a GIF playing simultaneously")

        grid._on_tile_clicked(3)          # "Loading corta arriba" -> DMX 12
        _assert(dialog.scene_faders.values()[4] == 12,
                f"picking one writes its dmxRange start "
                f"({dialog.scene_faders.values()[4]})")
        _assert(dialog.get_values().channels["animation"] == 12,
                "and it reaches the result")
        for popover in popovers:
            popover.close()
    finally:
        dialog.close()


def test_the_colour_wheel_is_offered_only_where_a_hue_can_land(qapp):
    print("test_the_colour_wheel_is_offered_only_where_a_hue_can_land")
    from PyQt6.QtGui import QColor
    from ui.fixture.affordances import apply_color, current_color, wheel_applies

    led, blinder = resolve_fixture("ledbar"), resolve_fixture("blinder")
    _assert(wheel_applies(led), "a fixture that mixes RGB gets a wheel")
    _assert(not wheel_applies(blinder),
            "a whites-only blinder does not — solve_color has nowhere to put "
            "a hue, so the wheel would quietly do nothing")

    values = [0] * led.channel_count
    out = apply_color(led, values, QColor(255, 120, 0))
    _assert(out[1:4] == [0, 120, 255],
            f"fitted onto the fixture's own emitter order ({out})")
    _assert(current_color(led, out) == (255, 120, 0),
            "and reads back through the same model the stage renders with")


def test_the_wheel_writes_through_the_dialog(qapp):
    print("test_the_wheel_writes_through_the_dialog")
    from PyQt6.QtGui import QColor

    led = resolve_fixture("ledbar")
    fields = [c.field for c in led.channels]
    dialog = ClipEditDialog.for_scene(
        fdef=led, type_name="ledbar", channel=0, dmx_start=1, name="S",
        channels={"brightness": 255}, present={"brightness"}, time=1.0)
    try:
        dialog.show()
        _assert(dialog.color_row.isVisible(), "the wheel is on the scene page")
        dialog._on_color_picked(QColor(255, 120, 0))
        channels = dialog.get_values().channels
        _assert(channels["red"] == 255 and channels["green"] == 120,
                f"picking a colour moves the faders ({channels})")
        _assert("brightness" in channels,
                "without disturbing the channel that was already carried")
    finally:
        dialog.close()

    blinder_dialog = _blinder_scene_dialog()
    try:
        blinder_dialog.show()
        _assert(not blinder_dialog.color_row.isVisible(),
                "and stays hidden on a fixture with no hue to set")
    finally:
        blinder_dialog.close()


# ---------------------------------------------------------------------------
# Live output.
# ---------------------------------------------------------------------------
def _drain(dialog, sent):
    """Fire the coalescing timer without spinning an event loop."""
    if dialog._live_timer.isActive():
        dialog._live_timer.stop()
        dialog._emit_live()
    return sent


def test_live_output_is_off_until_armed(qapp):
    print("test_live_output_is_off_until_armed")
    dialog = _blinder_scene_dialog()
    sent = []
    dialog.live_messages.connect(lambda m: sent.append(m))
    try:
        _assert(not dialog.live_check.isChecked(),
                "the toggle is off on open — this can be opened mid-show")
        dialog.scene_faders[3].slider.setValue(200)
        _drain(dialog, sent)
        _assert(sent == [], f"nothing goes to the rig while it is off ({sent})")

        dialog.live_check.setChecked(True)
        _drain(dialog, sent)
        _assert(len(sent) == 1, f"arming sends the current look ({len(sent)})")
        dialog.scene_faders[4].slider.setValue(64)
        _drain(dialog, sent)
        _assert(len(sent) == 2, "and each edit sends again")
    finally:
        dialog.close()


def test_live_messages_lead_with_take_manual(qapp):
    """A bare channel set is overwritten by the next auto frame, so the
    audition would show nothing without note 48 in front."""
    print("test_live_messages_lead_with_take_manual")
    dialog = _blinder_scene_dialog()
    try:
        messages = dialog._live_messages()
        _assert(messages[0] == (1, note_layout.TAKE_MANUAL, 64),
                f"a scene leads with take-manual ({messages[0]})")
        notes = [n for _c, n, _v in messages[1:]]
        _assert(notes == list(range(1, 9)),
                f"then one note per carried channel ({notes})")
        _assert(all(c == 1 for c, _n, _v in messages),
                "all routed to the fixture's own MIDI channel")
    finally:
        dialog.close()

    fdef = resolve_fixture("ledbar")
    dialog = ClipEditDialog.for_clip(
        fdef=fdef, type_name="ledbar", channel=0, dmx_start=1,
        note=4, velocity=100, time=1.0)
    try:
        messages = dialog._live_messages()
        _assert(messages == [(0, note_layout.TAKE_MANUAL, 64), (0, 4, 100)],
                f"a channel verb leads with it too ({messages})")
    finally:
        dialog.close()

    dialog = ClipEditDialog.for_clip(
        fdef=fdef, type_name="ledbar", channel=0, dmx_start=1,
        note=36, velocity=64, time=1.0)        # bank select
    try:
        messages = dialog._live_messages()
        _assert(messages == [(0, 36, 64)],
                f"but a bank select is its own command ({messages})")
    finally:
        dialog.close()


def test_cancel_reverts_exactly_once(qapp):
    print("test_cancel_reverts_exactly_once")
    dialog = _blinder_scene_dialog()
    sent = []
    dialog.live_messages.connect(lambda m: sent.append(m))
    dialog.live_check.setChecked(True)
    _drain(dialog, sent)
    original = list(sent[-1])

    dialog.scene_faders[3].slider.setValue(255)
    _drain(dialog, sent)
    _assert(sent[-1] != original, "the edit changed what is on the rig")

    dialog.reject()
    _assert(sent[-1] == original,
            "rejecting re-sends what the clip held when it opened")
    before = len(sent)
    dialog.close()                       # closeEvent must not fire it again
    _assert(len(sent) == before,
            f"and closing does not send a second time ({len(sent) - before})")


def test_accept_leaves_the_committed_look_on_the_rig(qapp):
    print("test_accept_leaves_the_committed_look_on_the_rig")
    dialog = _blinder_scene_dialog()
    sent = []
    dialog.live_messages.connect(lambda m: sent.append(m))
    dialog.live_check.setChecked(True)
    dialog.scene_faders[3].slider.setValue(255)
    dialog.accept()
    _assert(sent[-1] == dialog._live_messages(),
            "accepting leaves the accepted values on the rig")
    before = len(sent)
    dialog.close()
    _assert(len(sent) == before, "and does not then revert them")


def test_playing_locks_out_live_output(qapp):
    print("test_playing_locks_out_live_output")
    dialog = _blinder_scene_dialog()
    sent = []
    dialog.live_messages.connect(lambda m: sent.append(m))
    try:
        dialog.live_check.setChecked(True)
        _drain(dialog, sent)
        armed = len(sent)

        dialog.set_transport_playing(True)
        _assert(not dialog.live_check.isChecked(), "playing disarms the toggle")
        _assert(not dialog.live_check.isEnabled(), "and locks it out")
        _assert(len(sent) == armed + 1,
                "disarming reverted once, as any disarm does")

        before = len(sent)
        dialog.scene_faders[5].slider.setValue(90)
        _drain(dialog, sent)
        _assert(len(sent) == before,
                "and no edit reaches the rig while the sequencer runs")

        dialog.set_transport_playing(False)
        _assert(dialog.live_check.isEnabled(), "stopping restores the toggle")
    finally:
        dialog.close()


def test_a_locked_lane_never_sends(qapp):
    print("test_a_locked_lane_never_sends")
    dialog = ClipEditDialog.for_scene(
        fdef=resolve_fixture("blinder"), type_name="blinder", channel=1,
        dmx_start=33, name="B", channels=BLINDER_SCENE,
        present=set(BLINDER_SCENE), time=4.0, locked=True)
    sent = []
    dialog.live_messages.connect(lambda m: sent.append(m))
    try:
        _assert(not dialog.live_check.isEnabled(),
                "the toggle is dead on a locked lane")
        dialog._push_live()
        _drain(dialog, sent)
        _assert(sent == [], "and nothing can be pushed")
    finally:
        dialog.close()


# ---------------------------------------------------------------------------
# The fixture library, both directions.
# ---------------------------------------------------------------------------
SCHEMA = ("https://raw.githubusercontent.com/OpenLightingProject/"
          "open-fixture-library/master/schemas/fixture.json")

# The registry key is the OFL document's own name, not the filename.
PAR_TYPE = "Par 4"


def _par_document():
    return {
        "$schema": SCHEMA, "name": "Par 4", "categories": ["Color Changer"],
        "meta": {"authors": ["test"], "createDate": "2026-01-01",
                 "lastModifyDate": "2026-01-01"},
        "availableChannels": {
            "brightness": {"capability": {"type": "Intensity"}},
            "red": {"capability": {"type": "ColorIntensity", "color": "Red"}},
            "green": {"capability": {"type": "ColorIntensity", "color": "Green"}},
            "blue": {"capability": {"type": "ColorIntensity", "color": "Blue"}},
        },
        "modes": [{"name": "4ch", "shortName": "4c",
                   "channels": ["brightness", "red", "green", "blue"]}],
    }


PAR_SIDECAR = {"tdmx": 1, "modes": {"*": {"banks": {
    "FILLS": [{"name": "Red wash", "brightness": 255, "red": 255,
               "green": 0, "blue": 0, "song_parts": ["CHORUS"]}],
}}}}


def _with_par_library(body):
    """Run `body()` against a throwaway fixtures tree, so a sidecar write in a
    test can never reach the repo's shipped definitions."""
    orig_shipped = fixture_library.fixtures_dir_shipped
    orig_user = fixture_library.fixtures_dir_user
    try:
        with tempfile.TemporaryDirectory() as tmp:
            shipped, user = Path(tmp) / "shipped", Path(tmp) / "user"
            shipped.mkdir()
            user.mkdir()
            fixture_library.fixtures_dir_shipped = lambda: str(shipped)
            fixture_library.fixtures_dir_user = lambda: user
            clear_cache()
            (shipped / "par4.json").write_text(json.dumps(_par_document()))
            (shipped / "par4.tdmx.json").write_text(json.dumps(PAR_SIDECAR))
            load_fixture_defs(force_reload=True)
            body(shipped)
    finally:
        fixture_library.fixtures_dir_shipped = orig_shipped
        fixture_library.fixtures_dir_user = orig_user
        clear_cache()


def test_loading_a_stored_scene_fills_the_faders(qapp):
    print("test_loading_a_stored_scene_fills_the_faders")

    def body(_shipped):
        fdef = resolve_fixture(PAR_TYPE)
        dialog = ClipEditDialog.for_scene(
            fdef=fdef, type_name=PAR_TYPE, channel=0, dmx_start=1, name="",
            channels={"brightness": 10}, present={"brightness"}, time=1.0)
        try:
            banks = [a.text() for a in dialog.load_menu.actions()]
            _assert(banks == ["FILLS"], f"a submenu per bank ({banks})")
            scenes = [a.text() for a in dialog.load_menu.actions()[0].menu().actions()]
            _assert(scenes == ["Red wash"], f"and the bank's scenes ({scenes})")

            _assert(dialog.get_values().channels == {"brightness": 10},
                    "before loading, only the carried channel")
            dialog.load_menu.actions()[0].menu().actions()[0].trigger()
            loaded = dialog.get_values().channels
            _assert(loaded == {"brightness": 255, "red": 255,
                               "green": 0, "blue": 0},
                    f"every channel the stored scene names joins ({loaded})")
            _assert(dialog.scene_name.text() == "Red wash",
                    "and an unnamed clip takes the scene's name")
        finally:
            dialog.close()

    _with_par_library(body)


def test_saving_writes_the_sidecar_and_announces_it(qapp):
    print("test_saving_writes_the_sidecar_and_announces_it")
    from PyQt6.QtWidgets import QInputDialog, QMessageBox

    def body(shipped):
        fdef = resolve_fixture(PAR_TYPE)
        dialog = ClipEditDialog.for_scene(
            fdef=fdef, type_name=PAR_TYPE, channel=0, dmx_start=1, name="Blue hit",
            channels={"brightness": 255, "blue": 200},
            present={"brightness", "blue"}, time=1.0)
        announced = []
        dialog.library_changed.connect(lambda: announced.append(True))

        orig_item, orig_text = QInputDialog.getItem, QInputDialog.getText
        QInputDialog.getItem = staticmethod(lambda *a, **k: ("FILLS", True))
        QInputDialog.getText = staticmethod(lambda *a, **k: ("Blue hit", True))
        try:
            dialog._save_to_library()
        finally:
            QInputDialog.getItem, QInputDialog.getText = orig_item, orig_text
            dialog.close()

        raw = json.loads((shipped / "par4.tdmx.json").read_text())
        names = [s["name"] for s in raw["modes"]["*"]["banks"]["FILLS"]]
        _assert(names == ["Red wash", "Blue hit"],
                f"appended to the bank, never inserted ({names})")
        saved = raw["modes"]["*"]["banks"]["FILLS"][1]
        _assert(saved["blue"] == 200 and saved["brightness"] == 255,
                "with the faders' values")
        _assert(raw["modes"]["*"]["banks"]["FILLS"][0]["song_parts"] == ["CHORUS"],
                "and the existing scene's unmodelled keys survive")
        _assert(announced == [True],
                "library_changed fired so the clip library can reload")
        _assert("Saved" in dialog.status_label.text(),
                f"the status line confirms ({dialog.status_label.text()!r})")

    _with_par_library(body)


def test_overwriting_a_scene_keeps_its_index_and_tags(qapp):
    """A saved .mid binds a scene by INDEX, so overwrite-in-place is the only
    collision answer that leaves existing shows alone."""
    print("test_overwriting_a_scene_keeps_its_index_and_tags")
    from PyQt6.QtWidgets import QInputDialog, QMessageBox

    def body(shipped):
        fdef = resolve_fixture(PAR_TYPE)
        dialog = ClipEditDialog.for_scene(
            fdef=fdef, type_name=PAR_TYPE, channel=0, dmx_start=1, name="Red wash",
            channels={"brightness": 128, "red": 64, "green": 0, "blue": 0},
            present={"brightness", "red", "green", "blue"}, time=1.0)

        orig_item, orig_text = QInputDialog.getItem, QInputDialog.getText
        orig_q = QMessageBox.question
        QInputDialog.getItem = staticmethod(lambda *a, **k: ("FILLS", True))
        QInputDialog.getText = staticmethod(lambda *a, **k: ("Red wash", True))
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Yes)
        try:
            dialog._save_to_library()
        finally:
            QInputDialog.getItem, QInputDialog.getText = orig_item, orig_text
            QMessageBox.question = orig_q
            dialog.close()

        raw = json.loads((shipped / "par4.tdmx.json").read_text())
        scenes = raw["modes"]["*"]["banks"]["FILLS"]
        _assert(len(scenes) == 1, f"overwritten in place ({len(scenes)} scenes)")
        _assert(scenes[0]["brightness"] == 128 and scenes[0]["red"] == 64,
                "with the new values")
        _assert(scenes[0]["song_parts"] == ["CHORUS"],
                "and the tags the editor does not model are preserved")

    _with_par_library(body)


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)

    test_a_blinder_scene_opens_as_a_blinder(app)
    test_only_carried_and_touched_channels_are_submitted(app)
    test_the_picker_offers_only_droppable_verbs(app)
    test_a_fixture_without_banks_is_offered_no_scene_verbs(app)
    test_channels_past_the_addressable_limit_are_greyed(app)
    test_each_verb_class_gets_its_own_editor(app)
    test_velocity_encodings_round_trip(app)
    test_retyping_resets_the_velocity(app)
    test_a_new_clip_needs_a_verb_before_ok(app)
    test_the_add_path_can_start_a_scene(app)
    test_a_locked_lane_is_read_only(app)
    test_the_capability_grid_is_data_driven(app)
    test_the_picker_is_discoverable_and_opens_on_a_real_click(app)
    test_the_colour_wheel_is_offered_only_where_a_hue_can_land(app)
    test_the_wheel_writes_through_the_dialog(app)
    test_live_output_is_off_until_armed(app)
    test_live_messages_lead_with_take_manual(app)
    test_cancel_reverts_exactly_once(app)
    test_accept_leaves_the_committed_look_on_the_rig(app)
    test_playing_locks_out_live_output(app)
    test_a_locked_lane_never_sends(app)
    test_loading_a_stored_scene_fills_the_faders(app)
    test_saving_writes_the_sidecar_and_announces_it(app)
    test_overwriting_a_scene_keeps_its_index_and_tags(app)
    test_closing_drops_the_preview_bodies(app)
    test_open_and_close_repeatedly(app)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
