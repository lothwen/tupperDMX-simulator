"""
Characterization test: the app fits a 1280-wide display.

Both toolbars are wider than 1280 when everything they hold is shown, so each
one folds its low-priority half away on a narrow window — the top bar's SHOW
and VERSIONS cards into the hamburger menu, the timeline row's view controls
into a "View" popup. The mechanic that makes that possible is that a folding
bar reports its FOLDED minimum: a widget is never resized below its own
minimum, so a bar that advertised its full width would never receive the
resize event that tells it to fold.

Guarded here because the failure is silent — a section grown a few pixels wider
does not break anything until someone opens the app on a small screen.

The MainWindow half is not built here (nothing in the suite builds the real
window); both toolbars construct standalone, which is all these minimums need.
Nothing pumps the event loop either — offscreen Qt delivers resize events
synchronously, and a processEvents() loop collected after test_headless_smoke
segfaults (see tests/test_prefs_reload.py for the long version).

Offscreen Qt + a shared QApplication come from conftest.py (the `qapp` fixture).

Run with:
    uv run python tests/test_ui_minimums.py
"""

import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QMainWindow

from core.config import Config
from ui import theme

# The narrowest display the app supports, less the window chrome a manager
# takes off either side.
TARGET_WIDTH = 1280
BUDGET = 1240


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _styled(qapp):
    """The real theme, since every minimum here is padding and type. Restored
    by the fixture below so the sheet does not leak into other tests."""
    qapp.setStyleSheet(theme.load_qss())


@pytest.fixture(autouse=True)
def _restore_stylesheet(qapp):
    previous = qapp.styleSheet()
    yield
    qapp.setStyleSheet(previous)


def _toolbar(qapp):
    from ui.toolbar_manager import ToolbarManager
    _styled(qapp)
    window = QMainWindow()
    manager = ToolbarManager(Config())
    window.addToolBar(manager.create_toolbar(window))
    window.show()
    return window, manager


def _timeline_toolbar(qapp):
    from ui.timeline_toolbar import TimelineToolbar
    _styled(qapp)
    window = QMainWindow()
    bar = TimelineToolbar(QIcon(), QIcon(), QIcon())
    window.setCentralWidget(bar)
    window.show()
    return window, bar


def test_top_toolbar_folds_into_the_budget(qapp):
    print("test_top_toolbar_folds_into_the_budget")
    window, manager = _toolbar(qapp)

    folded = manager.container.minimumSizeHint().width()
    _assert(folded <= BUDGET,
            f"the folded toolbar needs {folded}px, within the {BUDGET}px budget")
    _assert(manager._full_min_width > folded,
            f"the full toolbar is wider ({manager._full_min_width}px) — if it "
            "were not, there would be nothing to fold")
    _assert(window.minimumSizeHint().width() <= TARGET_WIDTH,
            "the window it lives in fits 1280px")


def test_top_toolbar_folding_is_reversible(qapp):
    print("test_top_toolbar_folding_is_reversible")
    window, manager = _toolbar(qapp)

    window.resize(TARGET_WIDTH, 800)
    _assert(manager._compact, "a 1280px window folds the bar")
    _assert(not manager.show_section.isVisible()
            and not manager.versions_section.isVisible(),
            "SHOW and VERSIONS are gone")
    _assert(manager._compact_actions[0].isVisible(),
            "their menu stand-ins have appeared")

    window.resize(1920, 1080)
    _assert(not manager._compact, "a 1920px window brings the bar back")
    _assert(manager.show_section.isVisible()
            and manager.versions_section.isVisible(),
            "SHOW and VERSIONS are back")
    _assert(not manager._compact_actions[0].isVisible(),
            "the menu stand-ins have stood down")

    window.resize(TARGET_WIDTH, 800)
    _assert(manager._compact, "and it folds again — the two thresholds do not "
            "chase each other")


def test_timeline_toolbar_folds_into_the_budget(qapp):
    print("test_timeline_toolbar_folds_into_the_budget")
    window, bar = _timeline_toolbar(qapp)

    folded = bar.minimumSizeHint().width()
    _assert(folded <= BUDGET,
            f"the folded transport row needs {folded}px, within the "
            f"{BUDGET}px budget")
    _assert(bar._full_min_width > folded,
            f"the full row is wider ({bar._full_min_width}px)")


def test_timeline_view_controls_survive_folding(qapp):
    print("test_timeline_view_controls_survive_folding")
    window, bar = _timeline_toolbar(qapp)

    window.resize(TARGET_WIDTH, 200)
    _assert(bar._compact, "a 1280px window folds the view controls away")
    _assert(bar._view_button.isVisible(), "the View button stands in for them")
    _assert(bar.zoom_slider.window() is bar._view_popup,
            "the zoom slider moved into the popup — the same widget, so it "
            "keeps its value")

    bar.zoom_slider.setValue(250)
    window.resize(1920, 200)
    _assert(not bar._compact, "a wide window unfolds them")
    _assert(not bar._view_button.isVisible(), "the View button steps aside")
    _assert(bar.zoom_slider.value() == 250,
            "and the zoom the user set while folded survived the trip back")


def test_density_scaling_leaves_borders_alone(qapp):
    print("test_density_scaling_leaves_borders_alone")
    source = """
    QPushButton {
        font-size: 12px;
        padding: 8px 16px;
        border: 2px solid #202020;
        border-radius: 3px;
        min-width: 80px;
        margin: 0px -3px;
    }
    QLabel { font-size: 9px; }
    QFrame { max-width: 1px; }
    """
    scaled = theme.scale_qss(source, 0.85)

    _assert("font-size: 10px" in scaled, "12px type scales to 10px")
    _assert("padding: 7px 14px" in scaled, "padding scales")
    _assert("min-width: 68px" in scaled, "explicit minimums scale")
    _assert("border: 2px solid #202020" in scaled,
            "a border keeps its width — scaled down it would round away")
    _assert("border-radius: 3px" in scaled,
            "and a radius keeps its own, so corners stay in step with boxes")
    _assert("font-size: 8px" in scaled,
            "9px type floors at 8px rather than becoming unreadable")
    _assert("max-width: 1px" in scaled, "a 1px hairline floors at 1px")
    _assert("margin: 0px -3px" in scaled,
            "a bare 0 stays 0 and a negative offset keeps its sign")


def test_widget_sizes_follow_the_scale(qapp):
    print("test_widget_sizes_follow_the_scale")
    from ui.timeline_toolbar import TimelineToolbar

    # These sizes live in the stylesheet rather than in setFixedSize() calls
    # precisely so scaling reaches them — at 100% the transport clock's box was
    # 120px and its type 20px, so at 150% a box still 120px wide clipped the
    # "00:00.000" it exists to show.
    sizes = {}
    for scale in (1.0, 1.5):
        qapp.setStyleSheet(theme.scale_qss(theme.load_qss(), scale))
        bar = TimelineToolbar(QIcon(), QIcon(), QIcon())
        bar.ensurePolished()
        sizes[scale] = (bar.time_display.sizeHint().width(),
                        bar.bpm_spinbox.sizeHint().width())

    clock, bpm = sizes[1.0]
    big_clock, big_bpm = sizes[1.5]
    _assert(big_clock > clock,
            f"the transport clock grows with the scale ({clock} -> {big_clock})")
    _assert(big_bpm > bpm, f"so does the BPM box ({bpm} -> {big_bpm})")


def test_timeline_header_follows_the_scale(qapp):
    print("test_timeline_header_follows_the_scale")
    from timeline.manager import Timeline

    # The header strip is painted, not styled, so the stylesheet cannot reach
    # it — it takes the scale through its constructor instead. Without this a
    # lane's name is clipped by a 240px strip at any scale above ~1.25.
    _assert(Timeline(ui_scale=1.5).HEADER_WIDTH
            > Timeline(ui_scale=1.0).HEADER_WIDTH,
            "the timeline's track-header strip widens with the scale")
    _assert(Timeline().HEADER_WIDTH == Timeline.HEADER_WIDTH,
            "and defaults to the width it was designed at")


def test_dialogs_open_inside_the_screen(qapp):
    print("test_dialogs_open_inside_the_screen")
    from PyQt6.QtWidgets import QDialog
    from PyQt6.QtGui import QGuiApplication
    from ui.widgets.sizing import fit_to_screen

    available = QGuiApplication.primaryScreen().availableGeometry()

    small = QDialog()
    fit_to_screen(small, 200, 150)
    _assert((small.width(), small.height()) == (200, 150),
            "a size that already fits is left alone")

    huge = QDialog()
    fit_to_screen(huge, available.width() * 2, available.height() * 2)
    _assert(huge.width() <= available.width()
            and huge.height() <= available.height(),
            f"an oversized dialog is clamped into the "
            f"{available.width()}x{available.height()} screen instead of "
            "opening with its buttons off the bottom")


def test_scaling_is_a_no_op_at_one(qapp):
    print("test_scaling_is_a_no_op_at_one")
    source = "QPushButton { font-size: 12px; padding: 8px; }"
    _assert(theme.scale_qss(source, 1.0) == source,
            "scaling by 1.0 returns the sheet untouched")

    bigger = theme.scale_qss(source, 1.25)
    _assert("font-size: 15px" in bigger and "padding: 10px" in bigger,
            "scaling up works as well as down — the slider goes both ways")


def test_scale_is_clamped_to_its_range(qapp):
    print("test_scale_is_clamped_to_its_range")
    from core.config import Config, UI_SCALE_MIN, UI_SCALE_MAX

    for asked, expected in ((0.1, UI_SCALE_MIN), (99.0, UI_SCALE_MAX),
                            (1.15, 1.15)):
        parsed = Config()
        Config._apply_userprefs(parsed, {"window": {"ui_scale": asked}})
        _assert(abs(parsed.window.ui_scale - expected) < 1e-6,
                f"a hand-edited scale of {asked} loads as {expected}")

    _assert(Config().window.ui_scale == 1.0,
            "and the default is the size the interface was designed at")


def test_applying_the_same_scale_twice_is_free(qapp):
    print("test_applying_the_same_scale_twice_is_free")
    # The window only re-measures its toolbars when this returns True, and a
    # live prefs reload calls it for every window edit.
    theme.apply_scale(qapp, 1.0)
    _assert(theme.apply_scale(qapp, 0.9) is True, "a new scale is applied")
    _assert(theme.applied_scale() == 0.9, "and remembered")
    _assert(theme.apply_scale(qapp, 0.9) is False,
            "the same scale again does nothing rather than restyling the app")
    theme.apply_scale(qapp, 1.0)


def main():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    test_top_toolbar_folds_into_the_budget(app)
    test_top_toolbar_folding_is_reversible(app)
    test_timeline_toolbar_folds_into_the_budget(app)
    test_timeline_view_controls_survive_folding(app)
    test_density_scaling_leaves_borders_alone(app)
    test_widget_sizes_follow_the_scale(app)
    test_timeline_header_follows_the_scale(app)
    test_dialogs_open_inside_the_screen(app)
    test_scaling_is_a_no_op_at_one(app)
    test_scale_is_clamped_to_its_range(app)
    test_applying_the_same_scale_twice_is_free(app)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
