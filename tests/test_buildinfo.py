"""
Characterization test: the version a build shows.

The app's version is its git tag, never a constant. `git describe` decorates
that tag past a release ("-3-gabc1234", "-dirty"), and a frozen build that was
cut from a shallow checkout gets a bare hash instead. Settings shows the
release the build follows and About also names its channel, so a tester can
tell an rc from a stable release and from a dev build after either.

Run with:
    uv run python tests/test_buildinfo.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PyQt6.QtWidgets import QLabel

from core import buildinfo


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def test_release_of_strips_the_describe_suffix():
    print("test_release_of_strips_the_describe_suffix")
    cases = {
        "v1.1": "v1.1",
        "v1.1-rc1": "v1.1-rc1",
        "v1.1-rc1-3-gabc1234": "v1.1-rc1",
        "v1.1-rc1-3-gabc1234-dirty": "v1.1-rc1",
        "v1.0-161-g1f2ecef-dirty": "v1.0",
        "v1.0-alpha6": "v1.0-alpha6",
        "ad29540": "ad29540",
        "ad29540-dirty": "ad29540",
        "dev": "dev",
    }
    for tag, release in cases.items():
        _assert(buildinfo.release_of(tag) == release, f"{tag} -> {release}")


def _pill_text(tag):
    from ui.about_dialog import AboutDialog
    saved = buildinfo.GIT_TAG
    buildinfo.GIT_TAG = tag
    try:
        dialog = AboutDialog()
        return dialog.findChild(QLabel, "aboutPill").text()
    finally:
        buildinfo.GIT_TAG = saved


def test_about_pill_names_the_channel(qapp):
    print("test_about_pill_names_the_channel")
    stable = _pill_text("v1.1")
    _assert(">v1.1<" in stable and "STABLE" in stable, "a release is STABLE")

    rc = _pill_text("v1.1-rc1")
    _assert(">v1.1-rc1<" in rc and "· RC" in rc,
            "a release candidate keeps its suffix and is RC")

    past = _pill_text("v1.1-rc1-3-gabc1234-dirty")
    _assert(">v1.1-rc1<" in past and "DEV" in past,
            "a build past the rc names it, but is DEV")

    shallow = _pill_text("ad29540")
    _assert(">ad29540<" in shallow and "DEV" in shallow,
            "a bare hash is shown as it is, and is DEV")


def main():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    test_release_of_strips_the_describe_suffix()
    test_about_pill_names_the_channel(app)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
