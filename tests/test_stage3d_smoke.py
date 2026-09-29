# ABOUTME: Smoke test for the experimental 3D stage: it imports as a package, finds its
# ABOUTME: GLBs through asset_path, builds its scene without GL, and fails soft.
"""The experimental 3D stage (`stage/stage3d/`) ships in every build, so what
would break a frozen build must break here first: a bare import of a module
that used to sit in `scripts/`, or an asset folder not found through
`core.paths.asset_path`. No GL is touched until the view is shown, which is
what lets CI's offscreen platform run this.

Run with:
    uv run python tests/test_stage3d_smoke.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.config import shipped_fixtures


def test_the_3d_stage_builds_for_the_shipped_patch(qapp):
    from stage.stage3d import embedded

    view = embedded.create(shipped_fixtures())
    assert isinstance(view, embedded.EmbeddedBarView), "the scene builds from the shipped assets"
    assert view.stage.patch.summary(), "and places the shipped patch on it"
    view.dispose()


def test_a_missing_asset_fails_soft(qapp, monkeypatch):
    from stage.stage3d import bar, embedded

    with tempfile.TemporaryDirectory() as empty:
        # The prototype raises SystemExit for a missing GLB; the app must get
        # None back, not an exit.
        monkeypatch.setattr(bar, "ASSET_DIR", empty)
        assert embedded.create(shipped_fixtures()) is None


def _venue():
    from stage.stage3d import bar
    return bar, bar.Venue.from_asset(bar.load_glb(os.path.join(bar.ASSET_DIR, "venue.glb")))


def test_the_fly_camera_starts_at_the_photographs_view():
    import numpy as np
    bar, venue = _venue()
    # R and the first frame must still be the photograph's viewpoint — the
    # one the patch is placed by.
    for fly, photo in zip(bar.FlyCamera(venue).basis(), bar.OrbitCamera(venue).basis()):
        assert np.allclose(fly, photo, atol=1e-9)


def test_held_keys_fly_along_the_view_and_ease_to_rest():
    import numpy as np
    bar, venue = _venue()
    cam = bar.FlyCamera(venue)
    cam.look(200, 60)                        # turn right, look down
    start = cam.position.copy()
    for _ in range(30):                      # half a second of W
        cam.move(1, 0, dt=1 / 60)
    moved = cam.position - start
    assert moved[1] < 0 and np.allclose(moved / np.linalg.norm(moved), cam.forward()), \
        "W flies where the view points, down included"
    assert 0 < np.linalg.norm(cam.velocity) <= cam.SPEED, "easing up to flying speed"
    for _ in range(90):                      # let go
        cam.move(0, 0, dt=1 / 60)
    assert not cam.velocity.any(), "and gliding to rest"
    height = cam.position[1]
    for _ in range(30):                      # half a second of D
        cam.move(0, 1, dt=1 / 60)
    assert abs(cam.position[1] - height) < 1e-9, "A/D step aside level"
    for _ in range(1200):                    # twenty seconds into a wall
        cam.move(1, 1, dt=1 / 60)
    assert np.allclose(bar.inside_room(venue, cam.position, 0.3), cam.position), \
        "the room holds it in"


def test_mouse_look_hides_the_pointer_and_turns_on_at_the_border_without_warping(qapp):
    # The offscreen platform cannot move the pointer either: native Wayland's path.
    import time
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtWidgets import QApplication
    from stage.stage3d import embedded

    left, none = Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton
    view = embedded.create(shipped_fixtures())
    view.resize(800, 500)

    def send(kind, x, button, buttons):
        at = QPointF(x, 250)
        QApplication.sendEvent(view, QMouseEvent(kind, at, at, button, buttons,
                                                 Qt.KeyboardModifier.NoModifier))

    send(QEvent.Type.MouseButtonPress, 400, left, left)
    assert view.cursor().shape() == Qt.CursorShape.BlankCursor, "the pointer hides"
    assert not view._warping
    yaw = view.camera.yaw
    send(QEvent.Type.MouseMove, 850, none, left)         # dragged past the right edge
    dragged = view.camera.yaw
    assert dragged > yaw, "dragging right looks right"
    for _ in range(3):
        view._tick()
        time.sleep(0.03)
    assert view.camera.yaw > dragged, "and pinned out there, the view keeps turning"
    send(QEvent.Type.MouseButtonRelease, 850, left, none)
    assert view.cursor().shape() == Qt.CursorShape.ArrowCursor, "the pointer is back"
    released = view.camera.yaw
    time.sleep(0.03)
    view._tick()
    assert view.camera.yaw == released, "and the turning stops with the drag"
    view.dispose()


def main():
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))


if __name__ == "__main__":
    main()
