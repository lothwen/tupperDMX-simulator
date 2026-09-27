"""
Characterization test: the timeline toolbar's click panel (ui/click_popup.py).

Tempo and meter are a draft seeded from the song on open; "Set as song tempo"
only offers itself when the draft differs; free-run follows the draft once the
edits settle; accent and level are standing settings; TAP averages the taps.

Run with:
    uv run python tests/test_click_popup.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QPushButton

app = QApplication.instance() or QApplication([])

from ui.click_popup import ClickPopup, TapTempo


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _record(signal):
    got = []
    signal.connect(lambda *args: got.append(args))
    return got


def test_tap_tempo():
    print("test_tap_tempo")
    tap = TapTempo()
    _assert(tap.tap(10.0) is None, "one tap is not a tempo yet")
    _assert(tap.tap(10.5) == 120, "two taps half a second apart are 120 BPM")
    for t in (11.0, 11.5, 12.0, 12.5, 13.0):
        bpm = tap.tap(t)
    _assert(bpm == 120, "a steady rhythm stays put")
    _assert(tap.tap(13.6) == 114,          # (13.6 - 11.5) / 4 = 0.525 s
            "the last four intervals are averaged")
    _assert(tap.tap(20.0) is None, "a long pause starts over")
    _assert(tap.tap(20.25) == 240 and tap.tap(20.26) == 300,
            "and the result stays inside the app's range")


def test_opens_on_the_song_and_offers_to_set_only_a_change():
    print("test_opens_on_the_song_and_offers_to_set_only_a_change")
    popup = ClickPopup()
    anchor = QPushButton("anchor")
    popup.set_song_tempo(128, "3/4", True)
    popup.open_above(anchor)
    _assert(popup.draft() == (128, "3/4"), "the panel opens on the song's values")
    _assert(not popup.apply_button.isEnabled(), "nothing to set yet")

    popup.bpm_spin.setValue(132)
    _assert(popup.apply_button.isEnabled(), "a changed tempo can be set")
    applied = _record(popup.apply_requested)
    popup._meter_buttons["6/8"].click()
    popup.apply_button.click()
    _assert(applied == [(132, "6/8")], "Set as song tempo carries the draft")

    popup.hide()
    popup.open_above(anchor)
    _assert(popup.draft() == (128, "3/4"),
            "an unapplied draft is dropped when the panel reopens")

    popup.set_song_tempo(128, "3/4", False)
    popup.bpm_spin.setValue(90)
    _assert(not popup.apply_button.isEnabled(), "no song, nothing to set")
    popup.hide()


def test_free_run_follows_the_draft_once_it_settles():
    print("test_free_run_follows_the_draft_once_it_settles")
    popup = ClickPopup()
    popup.set_song_tempo(100, "4/4", True)
    popup.open_above(QPushButton())
    started = _record(popup.free_run_requested)
    stopped = _record(popup.free_run_stop_requested)

    popup.free_run_button.click()
    _assert(started == [(100, "4/4")], "Free-run plays the draft")
    popup.set_free_running(True)
    _assert(popup.free_run_button.text() == "Stop", "the button becomes Stop")

    for bpm in (101, 102, 103):
        popup.bpm_spin.setValue(bpm)
    _assert(len(started) == 1, "edits do not retime one by one")
    popup._retime.timeout.emit()
    _assert(started[-1] == (103, "4/4"), "the settled draft retimes it")

    popup.hide()
    popup.open_above(QPushButton())
    _assert(popup.draft() == (103, "4/4"), "a running free-run keeps its draft")

    popup.free_run_button.click()
    _assert(len(stopped) == 1, "Stop stops it")
    popup.set_free_running(False)
    _assert(not popup._retime.isActive(), "and no retime is left pending")
    popup.hide()


def test_the_song_owns_the_click_while_it_plays():
    print("test_the_song_owns_the_click_while_it_plays")
    popup = ClickPopup()
    popup.set_transport_playing(True)
    _assert(not popup.free_run_button.isEnabled(),
            "Free-run is unavailable while the song plays")
    popup.set_transport_playing(False)
    _assert(popup.free_run_button.isEnabled(), "and back once it stops")


def test_accent_and_level_are_mirrored_without_echo():
    print("test_accent_and_level_are_mirrored_without_echo")
    popup = ClickPopup()
    accents = _record(popup.accent_toggled)
    levels = _record(popup.level_changed)

    popup.set_accent(False)
    popup.set_level(63)
    _assert(accents == [] and levels == [],
            "reflecting the header or the prefs is not an edit")
    _assert(popup._level_db.text() == "-4dB",
            "the dB readout is the header's (63 % = -4 dB)")

    popup.accent_switch.click()
    popup.level_slider.setValue(50)
    _assert(accents == [(True,)] and levels == [(50,)], "user edits are emitted")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
