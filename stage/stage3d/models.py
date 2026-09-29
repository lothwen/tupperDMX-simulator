# ABOUTME: Procedural models for the patched fixtures the bar's GLBs do not cover —
# ABOUTME: pixel bars and LED panels, blinders, smoke machines — built at run time.
"""The patch's own bodies, for `bar.py`.

`build_assets.py` writes the room and the photograph's rig — a par
and a moving head — to GLB files. A show's patch can hold anything, so the
fixtures those two files do not cover are built here, at run time, from the
same primitives (imported, not copied), at the size each definition says the
fixture is. Nothing is written to disk.

Every model here is centred on its own middle, faces the audience (+Z) and
stands up (+Y); the scene places it by that centre and backs it onto the rig
plane. What emits is a quad in the material named `cells`, its UVs running
0..1 left to right and top to bottom as the audience sees it — the renderer
lights it from a small texture of the fixture's cells (a pixel bar's LEDs, a
blinder's lamps), exactly as the 2D stage paints a `MatrixCells` or a
`BulbArray`.
"""

from typing import Dict, List, Sequence, Tuple

import numpy as np

from stage.stage3d import build_assets as bba
from stage.stage3d.build_assets import Mesh, Node, Rx, T, cylinder, merge, oriented, rod, rounded_box

#: The emitting face: a dark diffuser the renderer adds each cell's light to.
#: Anything textured would drop its UVs in `build_assets.GLB`, which is
#: one reason these models never go through a GLB at all.
MATERIALS: Dict[str, dict] = dict(bba.MATERIALS)
MATERIALS["cells"] = dict(base=(0.035, 0.035, 0.04), rough=0.25)

# The frame round an emitting face, as a fraction of the body's shorter side —
# as `stage/profiles/array.py` frames its faces.
FRAME_FRACTION = 0.12

# A face sits this far proud of the housing it is laid on, so the two never
# fight over the same depth.
FACE_LIFT = 0.0008


# A blinder's lamp box, as a share of its width: the U-bracket takes the rest.
BLINDER_BOX = 0.84


def face_size(kind: str, dims_mm: Sequence[float]) -> Tuple[float, float]:
    """The emitting face of a `pixel` or `blinder` model, in metres (w, h)."""
    w, h, _d = (float(v) / 1000.0 for v in dims_mm)
    if kind == "blinder":
        w *= BLINDER_BOX
    frame = min(w, h) * FRAME_FRACTION
    return w - 2 * frame, h - 2 * frame


def face_quad(width: float, height: float, z: float) -> Mesh:
    """The emitting face: a quad at `z`, normal +Z, UVs 0..1 across and down."""
    x, y = width / 2.0, height / 2.0
    positions = [(-x, y, z), (x, y, z), (x, -y, z), (-x, -y, z)]
    uvs = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    return oriented(Mesh(positions, [(0.0, 0.0, 1.0)] * 4, uvs, None, [(0, 1, 2), (0, 2, 3)]))


def _root(name: str, dims_mm: Sequence[float]) -> Node:
    return Node(name, extras={"dimensions_mm": [float(d) for d in dims_mm]})


def build_pixel_panel(dims_mm: Sequence[float]) -> List[Node]:
    """One body of a pixel fixture: an LED bar's tube, a matrix, a wash panel,
    a strobe's face. A housing, and one face over nearly all of its front."""
    w, h, d = (float(v) / 1000.0 for v in dims_mm)
    short = min(w, h)
    root = _root("pixel_panel", dims_mm)
    root.add(rounded_box((w / 2, h / 2, d / 2), min(short, d) * 0.15), "fixture_body")
    root.add(face_quad(*face_size("pixel", dims_mm), d / 2 + FACE_LIFT), "cells")
    # The mounting plate the wall arm lands on.
    plate = min(0.03, short * 0.4)
    root.add(rounded_box((plate, plate, 0.004), 0.002, center=(0, 0, -d / 2 - 0.004)), "steel_black")
    return [root]


def build_blinder(dims_mm: Sequence[float]) -> List[Node]:
    """A blinder: a box of lamps in a U-bracket. The lamps are one face,
    lit a cell at a time, and the renderer draws each cell as a round COB."""
    w, h, d = (float(v) / 1000.0 for v in dims_mm)
    box_w = w * BLINDER_BOX
    root = _root("blinder", dims_mm)
    root.add(rounded_box((box_w / 2, h / 2, d / 2), min(box_w, h, d) * 0.12), "fixture_body")
    root.add(face_quad(*face_size("blinder", dims_mm), d / 2 + FACE_LIFT), "cells")
    arm = (w - box_w) / 2.0
    bracket = [rounded_box((arm * 0.35, h * 0.3, d * 0.28), arm * 0.2,
                           center=(side * (box_w / 2 + arm * 0.5), 0.0, 0.0)) for side in (-1, 1)]
    # The bracket's spine runs round the back, where the wall arm meets it.
    bracket.append(rounded_box((w / 2, arm * 0.35, arm * 0.35), arm * 0.2,
                               center=(0.0, 0.0, -d / 2 - arm * 0.4)))
    for side in (-1, 1):
        bracket.append(rod((side * (box_w / 2 + arm * 0.5), 0.0, 0.0),
                           (side * (box_w / 2 + arm * 0.5), 0.0, -d / 2 - arm * 0.4), arm * 0.3, 8))
    root.add(merge(bracket), "steel_black")
    return [root]


def build_machine(dims_mm: Sequence[float]) -> List[Node]:
    """A smoke machine or hazer: a box with its nozzle toward the audience and
    a carrying handle. Nothing on it emits light."""
    w, h, d = (float(v) / 1000.0 for v in dims_mm)
    root = _root("machine", dims_mm)
    root.add(rounded_box((w / 2, h / 2, d / 2), min(w, h, d) * 0.08), "fixture_body")
    nozzle = min(w, h) * 0.12
    root.add(cylinder(nozzle, 0.03, 20, nozzle * 0.2).transform(
        T(0.0, -h * 0.1, d / 2 + 0.03) @ Rx(np.pi / 2)), "galvanized")
    handle = [rod((side * w * 0.3, h / 2, 0.0), (side * w * 0.3, h / 2 + 0.05, 0.0), 0.008, 8)
              for side in (-1, 1)]
    handle.append(rod((-w * 0.3, h / 2 + 0.05, 0.0), (w * 0.3, h / 2 + 0.05, 0.0), 0.01, 10))
    root.add(merge(handle), "steel_black")
    return [root]


def build_pipe_stub(length: float = 0.36) -> List[Node]:
    """A short length of scaffold tube along X, centred: what a hung fixture's
    clamp grips when its rigging position is a wall arm rather than a pipe."""
    root = Node("pipe_stub")
    root.add(rod((-length / 2, 0.0, 0.0), (length / 2, 0.0, 0.0), bba.PIPE_RADIUS, 16), "steel_black")
    return [root]
