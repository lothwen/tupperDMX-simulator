#!/usr/bin/env python3
# ABOUTME: Builds the 3D bar stage's GLB assets — venue, backline, furniture and fixtures —
# ABOUTME: procedurally with numpy, textures included, into assets/models/bar/.
"""Generate the glTF 2.0 binaries `scripts/stage3d_bar.py` renders.

Run it:

    uv run python scripts/build_bar_assets.py

Every model is built here from a handful of primitives — rounded boxes,
surfaces of revolution, tubes swept along paths, bevelled extrusions — and every
texture is procedural (brick, floorboards, velour, tolex, grille cloth, hammered
bronze, a guitar's sunburst), so the assets can be regenerated, diffed and tuned
like code. The guitar's outlines are the one thing measured rather than made up:
spline points traced off a product photograph of a Stratocaster. No
Blender, no glTF library: numpy builds the arrays and `GLB` below writes the
container, because the format is small enough that a dependency would be the
larger thing.

What the files carry, beyond meshes and PBR materials, and what reads it:

  * **units are metres**, +Y up, and each prop's origin is on the floor under
    its own centre, so the scene layout places it with a translation alone;
  * `venue.glb`'s scene extras hold the room's dimensions and its pipe
    positions: the renderer reads the room from the asset, not from a copy;
  * the **house lights** — the downlights over the seating, the bar's pendants
    and its back-bar strip — are `KHR_lights_punctual` spots on the nodes that
    hold their lamps, so any glTF viewer shows them too. Their intensity is in
    the stage renderer's own arbitrary units, not candela;
  * **fixtures** have named nodes the rig animates: `yoke` turns about Y (pan),
    `body` / `head` about X (tilt), and an empty `lens` node marks the point the
    beam leaves from. Both hang, so their home aim is straight down (-Y);
  * the material named `lens` is lit by the renderer with its fixture's live
    beam colour, so it carries no emission of its own;
  * node extras `{"cast_shadows": false}` mark the room's shell, which has
    nothing inside it to shadow.
"""

import json
import math
import os
import struct
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "assets", "models", "bar")

# --------------------------------------------------------------------------
# The venue, in metres: phase 2b's axes (+X audience right, +Y up, +Z toward
# the audience), origin at the centre of the deck's front edge at deck height.
# --------------------------------------------------------------------------
HALF_WIDTH = 3.0
DECK_HEIGHT = 0.6
CEILING_Y = 3.4
BACK_WALL_Z = -3.4
CURTAIN_Z = -3.2
CURTAIN_TOP = 2.95
REAR_WALL_Z = 8.5
FRONT_PIPE = (3.25, -1.3)       # (y, z)
BACK_PIPE = (3.02, -2.85)
FOH_PIPE = (3.25, 2.3)
HOUSE_X, HOUSE_Z = 1.5, 3.4     # the two downlights over the seating
PIPE_RADIUS = 0.024             # 48 mm scaffold tube

# The house lights' colour (2700 K-ish, linear) and outputs, in the stage
# renderer's units.
WARM = (1.0, 0.342, 0.084)
HOUSE_OUTPUT = 4.0
PENDANT_OUTPUT = 5.5
BACKBAR_OUTPUT = 1.6


# ==========================================================================
# Transforms
# ==========================================================================

def T(x: float, y: float, z: float) -> np.ndarray:
    m = np.eye(4)
    m[:3, 3] = (x, y, z)
    return m


def S(x: float, y: Optional[float] = None, z: Optional[float] = None) -> np.ndarray:
    return np.diag([x, x if y is None else y, x if z is None else z, 1.0])


def Rx(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    m = np.eye(4)
    m[1:3, 1:3] = [[c, -s], [s, c]]
    return m


def Ry(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    m = np.eye(4)
    m[0, 0], m[0, 2], m[2, 0], m[2, 2] = c, s, -s, c
    return m


def Rz(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    m = np.eye(4)
    m[0:2, 0:2] = [[c, -s], [s, c]]
    return m


def align(src: Sequence[float], dst: Sequence[float]) -> np.ndarray:
    """The rotation taking direction `src` onto `dst` (Rodrigues)."""
    a = np.asarray(src, float) / np.linalg.norm(src)
    b = np.asarray(dst, float) / np.linalg.norm(dst)
    v = np.cross(a, b)
    c = float(np.dot(a, b))
    m = np.eye(4)
    if np.linalg.norm(v) < 1e-9:
        if c < 0:
            axis = np.cross(a, [1.0, 0.0, 0.0] if abs(a[0]) < 0.9 else [0.0, 1.0, 0.0])
            axis /= np.linalg.norm(axis)
            m[:3, :3] = 2.0 * np.outer(axis, axis) - np.eye(3)
        return m
    k = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    m[:3, :3] = np.eye(3) + k + k @ k * (1.0 / (1.0 + c))
    return m


def smoothstep(e0: float, e1: float, x):
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


# ==========================================================================
# Meshes
# ==========================================================================

class Mesh:
    """Indexed triangles with per-vertex normal, UV (metres) and colour."""

    def __init__(self, positions, normals, uvs=None, colors=None, indices=None):
        self.p = np.asarray(positions, np.float64).reshape(-1, 3)
        n = np.asarray(normals, np.float64).reshape(-1, 3)
        self.n = n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
        count = len(self.p)
        self.uv = np.zeros((count, 2)) if uvs is None else np.asarray(uvs, np.float64).reshape(-1, 2)
        self.c = None if colors is None else np.asarray(colors, np.float64).reshape(-1, 4)
        self.i = (np.arange(count).reshape(-1, 3) if indices is None
                  else np.asarray(indices, np.int64).reshape(-1, 3))

    def transform(self, m: np.ndarray) -> "Mesh":
        linear = m[:3, :3]
        out = Mesh(self.p @ linear.T + m[:3, 3], self.n @ np.linalg.inv(linear),
                   self.uv, self.c, self.i.copy())
        if np.linalg.det(linear) < 0:
            out.i = out.i[:, ::-1].copy()
        return out

    def painted(self, colors) -> "Mesh":
        """Per-vertex colour: a callable of positions, or one RGB(A) for all."""
        c = colors(self.p) if callable(colors) else np.broadcast_to(colors, (len(self.p), len(colors)))
        c = np.clip(np.asarray(c, np.float64), 0.0, 1.0)      # glTF: COLOR_0 is 0..1
        if c.shape[1] == 3:
            c = np.concatenate([c, np.ones((len(c), 1))], axis=1)
        return Mesh(self.p, self.n, self.uv, c, self.i)


def merge(meshes: Sequence[Mesh]) -> Mesh:
    meshes = [m for m in meshes if m is not None and len(m.i)]
    colored = any(m.c is not None for m in meshes)
    offset, idx = 0, []
    for m in meshes:
        idx.append(m.i + offset)
        offset += len(m.p)
    colors = None
    if colored:
        colors = np.concatenate([m.c if m.c is not None else np.ones((len(m.p), 4)) for m in meshes])
    return Mesh(np.concatenate([m.p for m in meshes]), np.concatenate([m.n for m in meshes]),
                np.concatenate([m.uv for m in meshes]), colors, np.concatenate(idx))


def oriented(mesh: Mesh) -> Mesh:
    """Wind every triangle to face the way its vertex normals do.

    The primitives below get their normals right analytically and their
    winding right by accident, so this makes the normals the authority.
    """
    p, n, i = mesh.p, mesh.n, mesh.i.copy()
    face = np.cross(p[i[:, 1]] - p[i[:, 0]], p[i[:, 2]] - p[i[:, 0]])
    flip = (face * (n[i[:, 0]] + n[i[:, 1]] + n[i[:, 2]])).sum(axis=1) < 0
    i[flip] = i[flip][:, [0, 2, 1]]
    return Mesh(mesh.p, mesh.n, mesh.uv, mesh.c, i)


def grid_indices(rows: int, cols: int) -> np.ndarray:
    r, c = np.meshgrid(np.arange(rows - 1), np.arange(cols - 1), indexing="ij")
    a = (r * cols + c).ravel()
    b = a + cols
    return np.concatenate([np.stack([a, b, b + 1], 1), np.stack([a, b + 1, a + 1], 1)])


def rounded_box(half, radius: float = 0.0, seg: int = 3, center=(0.0, 0.0, 0.0)) -> Mesh:
    """A box with every edge and corner rounded to `radius`.

    Each face is a grid whose lines crowd into the rounded margin, then every
    vertex is pushed onto the rounded surface: the flat middle keeps its face
    normal exactly, and the margins sweep 45 degrees each, meeting the next
    face's 45 at the edge.
    """
    h = np.asarray(half, float)
    r = float(min(radius, *h))

    def coords(extent: float) -> np.ndarray:
        if r <= 1e-6:
            return np.array([-extent, extent])
        phi = np.linspace(np.pi / 4, 0.0, seg + 1)
        lo = -extent + r - r * np.tan(phi)
        return np.concatenate([lo, -lo[::-1]])

    axis_coords = [coords(h[0]), coords(h[1]), coords(h[2])]
    inner = h - r
    faces = []
    for axis in range(3):
        a, b = [k for k in range(3) if k != axis]
        for sign in (-1.0, 1.0):
            ga, gb = np.meshgrid(axis_coords[a], axis_coords[b], indexing="ij")
            pts = np.zeros(ga.shape + (3,))
            pts[..., axis] = sign * h[axis]
            pts[..., a] = ga
            pts[..., b] = gb
            pts = pts.reshape(-1, 3)
            face_n = np.zeros(3)
            face_n[axis] = sign
            if r > 1e-6:
                core = np.clip(pts, -inner, inner)
                d = pts - core
                length = np.linalg.norm(d, axis=1, keepdims=True)
                nrm = np.where(length > 1e-9, d / np.maximum(length, 1e-12), face_n)
                pos = core + nrm * r
            else:
                nrm = np.tile(face_n, (len(pts), 1))
                pos = pts
            faces.append(Mesh(pos, nrm, np.stack([ga.ravel(), gb.ravel()], 1), None,
                              grid_indices(*ga.shape)))
    return oriented(merge(faces)).transform(T(*center))


def lathe(runs, seg: int = 32, arc=(0.0, 2.0 * np.pi)) -> Mesh:
    """A surface of revolution about +Y.

    `runs` is a list of profile polylines in (radius, height). Each run is
    smooth; the joins between runs are creases. Profiles go anticlockwise in
    the (r, y) plane — out along the bottom, up the side, in along the top —
    so the outward normal is on the right of the direction of travel.
    """
    a = np.linspace(arc[0], arc[1], seg + 1)
    sa, ca = np.sin(a), np.cos(a)
    meshes = []
    for run in runs:
        pts = np.asarray(run, float)
        if len(pts) < 2:
            continue
        t = np.gradient(pts, axis=0)
        nr, ny = t[:, 1].copy(), -t[:, 0].copy()
        length = np.hypot(nr, ny)
        length[length < 1e-12] = 1.0
        nr /= length
        ny /= length
        k = len(pts)
        rr = pts[:, 0][:, None]
        pos = np.stack([rr * sa, np.repeat(pts[:, 1][:, None], seg + 1, 1), rr * ca], -1)
        nrm = np.stack([nr[:, None] * sa, np.repeat(ny[:, None], seg + 1, 1), nr[:, None] * ca], -1)
        along = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(pts, axis=0).T))])
        uv = np.stack([np.repeat((a * max(pts[:, 0].max(), 1e-3))[None, :], k, 0),
                       np.repeat(along[:, None], seg + 1, 1)], -1)
        meshes.append(Mesh(pos.reshape(-1, 3), nrm.reshape(-1, 3), uv.reshape(-1, 2), None,
                           grid_indices(k, seg + 1)))
    return oriented(merge(meshes))


def disc(radius: float, seg: int = 32, up: bool = True) -> Mesh:
    return lathe([[(radius, 0.0), (0.0, 0.0)] if up else [(0.0, 0.0), (radius, 0.0)]], seg)


def cylinder(radius: float, half_height: float, seg: int = 24, bevel: float = 0.0) -> Mesh:
    r, h, b = radius, half_height, min(bevel, radius * 0.5, half_height * 0.5)
    if b <= 0:
        return lathe([[(0, -h), (r, -h)], [(r, -h), (r, h)], [(r, h), (0, h)]], seg)
    arc = [(r - b + b * math.sin(t), -h + b - b * math.cos(t)) for t in np.linspace(0, np.pi / 2, 4)]
    top = [(x, -y) for x, y in reversed(arc)]
    return lathe([[(0, -h)] + arc + top + [(0, h)]], seg)


def sphere(radius: float, seg: int = 24) -> Mesh:
    ts = np.linspace(0.0, np.pi, seg // 2 + 1)
    return lathe([[(radius * math.sin(t), -radius * math.cos(t)) for t in ts]], seg)


def catmull_rom(points, samples: int = 8, closed: bool = False) -> np.ndarray:
    p = np.asarray(points, float)
    if closed:
        ext = np.concatenate([p[-1:], p, p[:2]])
    else:
        ext = np.concatenate([2 * p[:1] - p[1:2], p, 2 * p[-1:] - p[-2:-1]])
    out = []
    for k in range(len(ext) - 3):
        p0, p1, p2, p3 = ext[k:k + 4]
        for t in np.linspace(0.0, 1.0, samples, endpoint=False):
            t2, t3 = t * t, t * t * t
            out.append(0.5 * ((2 * p1) + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2
                              + (-p0 + 3 * p1 - 3 * p2 + p3) * t3))
    if not closed:
        out.append(p[-1])
    return np.array(out)


def tube(path, radius, seg: int = 12, caps: bool = True, closed: bool = False,
         smooth: int = 0) -> Mesh:
    """A tube swept along a polyline, framed by parallel transport."""
    pts = np.asarray(path, float)
    if smooth:
        pts = catmull_rom(pts, smooth, closed)
    if closed:
        pts = np.concatenate([pts, pts[:1]])
    count = len(pts)
    if closed:
        t = np.roll(pts, -1, 0) - np.roll(pts, 1, 0)
        t[-1] = t[0] = pts[1] - pts[-2]
    else:
        t = np.gradient(pts, axis=0)
    t /= np.maximum(np.linalg.norm(t, axis=1, keepdims=True), 1e-12)
    radius = np.asarray(radius, float)
    if radius.ndim and len(radius) != count:          # a taper given at the path's own points
        radius = np.interp(np.linspace(0, 1, count), np.linspace(0, 1, len(radius)), radius)
    radii = np.broadcast_to(radius, (count,))
    ref = np.array([0.0, 1.0, 0.0]) if abs(t[0, 1]) < 0.9 else np.array([1.0, 0.0, 0.0])
    normal = np.cross(t[0], ref)
    normal /= np.linalg.norm(normal)
    frames = []
    for k in range(count):
        normal = normal - t[k] * np.dot(normal, t[k])
        normal /= max(np.linalg.norm(normal), 1e-12)
        frames.append(normal.copy())
    nrm_frame = np.array(frames)
    bin_frame = np.cross(t, nrm_frame)
    a = np.linspace(0.0, 2.0 * np.pi, seg + 1)
    offsets = (np.cos(a)[None, :, None] * nrm_frame[:, None, :]
               + np.sin(a)[None, :, None] * bin_frame[:, None, :])
    pos = pts[:, None, :] + radii[:, None, None] * offsets
    along = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(pts, axis=0), axis=1))])
    uv = np.stack([np.repeat((a * radii.mean())[None, :], count, 0),
                   np.repeat(along[:, None], seg + 1, 1)], -1)
    body = Mesh(pos.reshape(-1, 3), offsets.reshape(-1, 3), uv.reshape(-1, 2), None,
                grid_indices(count, seg + 1))
    parts = [body]
    if caps and not closed:
        for end, direction in ((0, -t[0]), (count - 1, t[-1])):
            ring = pos[end, :-1]
            centre = pts[end]
            p = np.concatenate([[centre], ring])
            n = np.tile(direction, (len(p), 1))
            idx = np.array([[0, 1 + k, 1 + (k + 1) % seg] for k in range(seg)])
            parts.append(Mesh(p, n, np.zeros((len(p), 2)), None, idx))
    return oriented(merge(parts))


def rod(a, b, radius: float, seg: int = 12, caps: bool = True) -> Mesh:
    return tube([a, b], radius, seg, caps)


def circle_path(radius: float, count: int = 48, start: float = 0.0, stop: float = 2 * np.pi):
    """Points on a circle in the XY plane."""
    ts = np.linspace(start, stop, count)
    return np.stack([radius * np.cos(ts), radius * np.sin(ts), np.zeros_like(ts)], 1)


def ear_clip(outline) -> np.ndarray:
    """Triangulate a simple polygon (XY, anticlockwise) by clipping ears."""
    pts = np.asarray(outline, float)
    remaining = list(range(len(pts)))
    triangles = []
    k = stall = 0
    while len(remaining) > 3:
        n = len(remaining)
        i0, i1, i2 = remaining[(k - 1) % n], remaining[k % n], remaining[(k + 1) % n]
        a, b, c = pts[i0], pts[i1], pts[i2]
        if (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]) > 1e-15:
            others = pts[[j for j in remaining if j not in (i0, i1, i2)]]
            inside = np.ones(len(others), bool)
            for p, q in ((a, b), (b, c), (c, a)):
                inside &= (q[0] - p[0]) * (others[:, 1] - p[1]) - (q[1] - p[1]) * (others[:, 0] - p[0]) >= 0
            if not inside.any():
                triangles.append((i0, i1, i2))
                remaining.pop(k % n)
                stall = 0
                continue
        k += 1
        stall += 1
        if stall > n:
            raise ValueError("ear_clip: the outline is not a simple anticlockwise polygon")
    triangles.append(tuple(remaining))
    return np.array(triangles)


def rounded_slab(outline, half_thickness: float, bevel: float, arc_seg: int = 3):
    """Extrude a simple outline (XY, anticlockwise) into a slab along Z with
    rounded edges. Returns (front cap, back cap, sides) so each can take its
    own material; the caps are the outline ear-clipped, UVs its XY in metres."""
    outline = np.asarray(outline, float)
    tangent = np.roll(outline, -1, 0) - np.roll(outline, 1, 0)
    outward = np.stack([tangent[:, 1], -tangent[:, 0]], 1)
    outward /= np.linalg.norm(outward, axis=1, keepdims=True)
    inner = outline - outward * bevel
    count = len(outline)
    triangles = ear_clip(inner)

    def cap(z: float, facing: float) -> Mesh:
        p = np.concatenate([inner, np.full((count, 1), z)], 1)
        n = np.tile([0.0, 0.0, facing], (count, 1))
        return oriented(Mesh(p, n, inner, None, triangles))

    flat = half_thickness - bevel
    angles = np.concatenate([np.linspace(-np.pi / 2, 0, arc_seg + 1),
                             np.linspace(0, np.pi / 2, arc_seg + 1)])
    centres = np.concatenate([np.full(arc_seg + 1, -flat), np.full(arc_seg + 1, flat)])
    pos, nrm = [], []
    for ang, zc in zip(angles, centres):
        ring = inner + outward * (bevel * math.cos(ang))
        pos.append(np.concatenate([ring, np.full((count, 1), zc + bevel * math.sin(ang))], 1))
        nrm.append(np.concatenate([outward * math.cos(ang), np.full((count, 1), math.sin(ang))], 1))
    pos_arr = np.stack(pos)                       # (rings, count, 3)
    pos_arr = np.concatenate([pos_arr, pos_arr[:, :1]], 1)
    nrm_arr = np.concatenate([np.stack(nrm), np.stack(nrm)[:, :1]], 1)
    closed_outline = np.concatenate([outline, outline[:1]])
    along = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(closed_outline, axis=0), axis=1))])
    uv = np.stack([np.repeat(along[None, :], len(angles), 0),
                   np.repeat(np.arange(len(angles))[:, None] * 0.01, count + 1, 1)], -1)
    sides = oriented(Mesh(pos_arr.reshape(-1, 3), nrm_arr.reshape(-1, 3), uv.reshape(-1, 2), None,
                          grid_indices(len(angles), count + 1)))
    return cap(half_thickness, 1.0), cap(-half_thickness, -1.0), sides


def rounded_rect(hx: float, hy: float, r: float, seg: int = 4) -> np.ndarray:
    """An anticlockwise rectangle outline with its corners rounded to r."""
    out = []
    for cx, cy, start in ((hx - r, -hy + r, -np.pi / 2), (hx - r, hy - r, 0.0),
                          (-hx + r, hy - r, np.pi / 2), (-hx + r, -hy + r, np.pi)):
        for t in np.linspace(start, start + np.pi / 2, seg + 1):
            out.append((cx + r * math.cos(t), cy + r * math.sin(t)))
    return np.array(out)


def outline_mm(points, samples: int = 5) -> np.ndarray:
    """A closed spline through control points given in millimetres: metres,
    anticlockwise."""
    p = catmull_rom(np.asarray(points, float) / 1000.0, samples, closed=True)
    area = np.sum(p[:, 0] * np.roll(p[:, 1], -1) - np.roll(p[:, 0], -1) * p[:, 1])
    return p if area > 0 else p[::-1].copy()


def tapered(mesh: Mesh, y0: float, y1: float, k0: float, k1: float) -> Mesh:
    """Scale X by a factor running linearly from k0 at y0 to k1 at y1."""
    k = np.interp(mesh.p[:, 1], [y0, y1], [k0, k1])
    p, n = mesh.p.copy(), mesh.n.copy()
    p[:, 0] *= k
    n[:, 0] /= k
    return Mesh(p, n, mesh.uv, mesh.c, mesh.i)


def plane(origin, u_axis, v_axis, nu: int, nv: int, normal) -> Mesh:
    """A subdivided rectangle; UVs in metres along its two edges."""
    o, u, v = (np.asarray(x, float) for x in (origin, u_axis, v_axis))
    su, sv = np.meshgrid(np.linspace(0, 1, nu + 1), np.linspace(0, 1, nv + 1), indexing="ij")
    p = o + su[..., None] * u + sv[..., None] * v
    uv = np.stack([su * np.linalg.norm(u), sv * np.linalg.norm(v)], -1)
    n = np.tile(np.asarray(normal, float), ((nu + 1) * (nv + 1), 1))
    return oriented(Mesh(p.reshape(-1, 3), n, uv.reshape(-1, 2), None, grid_indices(nu + 1, nv + 1)))


# ==========================================================================
# Noise and textures
# ==========================================================================

def hash_noise(x, y):
    """A cheap smooth value noise at arbitrary points, for vertex colours."""
    xi, yi = np.floor(x), np.floor(y)
    xf, yf = x - xi, y - yi
    xf, yf = xf * xf * (3 - 2 * xf), yf * yf * (3 - 2 * yf)

    def h(i, j):
        return np.modf(np.sin(i * 127.1 + j * 311.7) * 43758.5453)[0] % 1.0

    return ((h(xi, yi) * (1 - xf) + h(xi + 1, yi) * xf) * (1 - yf)
            + (h(xi, yi + 1) * (1 - xf) + h(xi + 1, yi + 1) * xf) * yf)


def tile_noise(h: int, w: int, cells_y: int, cells_x: int, rng) -> np.ndarray:
    """Smooth value noise that wraps at the image edges, so textures tile."""
    grid = rng.random((cells_y, cells_x))
    y = np.arange(h) * cells_y / h
    x = np.arange(w) * cells_x / w
    y0, x0 = np.floor(y).astype(int), np.floor(x).astype(int)
    fy, fx = y - y0, x - x0
    fy, fx = (fy * fy * (3 - 2 * fy))[:, None], (fx * fx * (3 - 2 * fx))[None, :]
    y1, x1 = (y0 + 1) % cells_y, (x0 + 1) % cells_x
    g00, g01 = grid[y0][:, x0], grid[y0][:, x1]
    g10, g11 = grid[y1][:, x0], grid[y1][:, x1]
    return (g00 * (1 - fx) + g01 * fx) * (1 - fy) + (g10 * (1 - fx) + g11 * fx) * fy


def tile_fbm(h: int, w: int, cells_y: int, cells_x: int, octaves: int, rng) -> np.ndarray:
    total, amp, norm = np.zeros((h, w)), 0.5, 0.0
    for _ in range(octaves):
        total += amp * tile_noise(h, w, cells_y, cells_x, rng)
        norm += amp
        cells_y, cells_x, amp = cells_y * 2, cells_x * 2, amp * 0.5
    return total / norm


def to_srgb8(linear) -> np.ndarray:
    c = np.clip(linear, 0.0, 1.0)
    srgb = np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(c, 1 / 2.4) - 0.055)
    return np.round(srgb * 255).astype(np.uint8)


def normal_map(height: np.ndarray, texel_u: float, texel_v: float, strength: float = 1.0) -> np.ndarray:
    """Tangent-space normals from a height field, glTF's convention: +X right,
    +Y up the image (which is toward decreasing V), +Z out of the surface."""
    du = (np.roll(height, -1, 1) - np.roll(height, 1, 1)) / (2 * texel_u)
    dv = (np.roll(height, -1, 0) - np.roll(height, 1, 0)) / (2 * texel_v)
    n = np.stack([-du * strength, dv * strength, np.ones_like(height)], -1)
    n /= np.linalg.norm(n, axis=-1, keepdims=True)
    return np.round((n * 0.5 + 0.5) * 255).astype(np.uint8)


def png(image: np.ndarray) -> bytes:
    """Encode an (H, W, C) uint8 array as PNG, with zlib and nothing else."""
    image = np.ascontiguousarray(image, np.uint8)
    if image.ndim == 2:
        image = image[..., None]
    h, w, c = image.shape
    kind = {1: 0, 2: 4, 3: 2, 4: 6}[c]
    raw = b"".join(b"\x00" + image[row].tobytes() for row in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, kind, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def tex_brick():
    """1024 px over 0.9 m: four bricks by twelve courses, running bond."""
    size, tile = 1024, 0.9
    rng = np.random.default_rng(3)
    px = tile / size
    yy, xx = np.mgrid[0:size, 0:size] * px
    pitch_x, pitch_y = 0.225, 0.075
    row = np.floor(yy / pitch_y).astype(int)
    shifted = xx / pitch_x + 0.5 * (row % 2)
    col = np.floor(shifted).astype(int) % 4
    fx = (shifted % 1.0) * pitch_x
    fy = (yy / pitch_y % 1.0) * pitch_y
    edge = np.minimum(np.minimum(fx, pitch_x - fx), np.minimum(fy, pitch_y - fy))
    face = smoothstep(0.004, 0.009, edge)
    tones = rng.random((12, 4))
    tone = tones[row % 12, col]
    blotch = tile_fbm(size, size, 12, 12, 4, rng)
    grain = tile_noise(size, size, 320, 320, rng)
    fired = (np.array([0.16, 0.085, 0.055]) * (1 - tone[..., None])
             + np.array([0.26, 0.15, 0.095]) * tone[..., None])
    fired = fired * (0.72 + 0.5 * blotch[..., None]) * (0.9 + 0.2 * grain[..., None])
    overfired = (np.modf(tone * 7.13)[0] > 0.86)[..., None]
    fired = np.where(overfired, fired * 0.55, fired)
    mortar = np.array([0.12, 0.105, 0.088]) * (0.8 + 0.4 * grain[..., None])
    albedo = mortar * (1 - face[..., None]) + fired * face[..., None]
    height = face * (0.006 + 0.002 * blotch) + 0.0012 * grain
    return {"brick_albedo": png(to_srgb8(albedo)),
            "brick_normal": png(normal_map(height, px, px, 0.6))}


def tex_planks():
    """512 x 1024 px over 1.12 m x 3.2 m: eight boards, their joints staggered."""
    w, h = 512, 1024
    tile_u, tile_v, board_w = 1.12, 3.2, 0.14
    rng = np.random.default_rng(11)
    pu, pv = tile_u / w, tile_v / h
    vv, uu = np.mgrid[0:h, 0:w]
    uu, vv = uu * pu, vv * pv
    board = np.floor(uu / board_w).astype(int)
    per_board = rng.integers(1, 4, 8)            # 1-3 lengths per repeat, so it tiles
    offset = rng.random(8) * tile_v
    length = tile_v / per_board[board]
    along = (vv + offset[board]) / length
    seg = np.floor(along).astype(int) % per_board[board]
    fx = (uu / board_w % 1.0) * board_w
    fz = (along % 1.0) * length
    edge = np.minimum(np.minimum(fx, board_w - fx), np.minimum(fz, length - fz))
    tones = rng.random((8, 3))
    tone = tones[board, seg]
    grain = (0.6 * tile_noise(h, w, 6, 96, rng) + 0.4 * tile_noise(h, w, 24, 320, rng))
    wear = tile_fbm(h, w, 4, 2, 4, rng)
    col = np.array([0.085, 0.064, 0.05]) * ((0.65 + 0.7 * tone) * (0.7 + 0.5 * grain))[..., None]
    col = col * (0.3 + 0.7 * smoothstep(0.0, 0.003, edge))[..., None]
    col = col * (0.85 + 0.35 * wear)[..., None]
    rough = 0.5 + 0.35 * wear
    mr = np.stack([np.full_like(rough, 255), np.round(rough * 255), np.zeros_like(rough)], -1)
    height = 0.002 * smoothstep(0.0, 0.004, edge) + 0.00015 * grain
    return {"planks_albedo": png(to_srgb8(col)),
            "planks_normal": png(normal_map(height, pu, pv, 0.8)),
            "planks_mr": png(mr.astype(np.uint8))}


def tex_small():
    rng = np.random.default_rng(5)
    out = {}
    # Tolex: pebbled vinyl, 256 px over 12 cm.
    pebble = tile_fbm(256, 256, 40, 40, 3, rng)
    out["tolex_normal"] = png(normal_map(pebble * 0.0004, 0.12 / 256, 0.12 / 256, 1.0))
    # Grille cloth: a basket weave, as a grey the material tints.
    yy, xx = np.mgrid[0:256, 0:256] / 256.0
    warp = np.sin(xx * 2 * np.pi * 16) * np.sin(yy * 2 * np.pi * 16)
    weave = 0.72 + 0.2 * (warp > 0) + 0.1 * tile_noise(256, 256, 64, 64, rng)
    out["weave"] = png(to_srgb8(np.repeat(weave[..., None], 3, -1)))
    # Velour: vertical pile, streaky.
    pile = 0.78 + 0.22 * (0.7 * tile_noise(256, 256, 3, 96, rng) + 0.3 * tile_noise(256, 256, 8, 200, rng))
    out["velour"] = png(to_srgb8(np.repeat(pile[..., None], 3, -1)))
    # Hammered bronze: jittered dimples, wrapped so the tile repeats.
    size, tile = 256, 0.2
    height = np.zeros((size, size))
    yy, xx = np.mgrid[0:size, 0:size] * (tile / size)
    cells = 9
    for cy in range(cells):
        for cx in range(cells):
            px = (cx + rng.random()) * tile / cells
            py = (cy + rng.random()) * tile / cells
            dx = (xx - px + tile / 2) % tile - tile / 2
            dy = (yy - py + tile / 2) % tile - tile / 2
            height -= 0.0012 * np.exp(-(dx * dx + dy * dy) / (0.009 ** 2))
    out["hammered_normal"] = png(normal_map(height, tile / size, tile / size, 1.0))
    return out


# The guitar's burst covers the body's outline in its own frame (see
# `build_guitar`), the image's top-left corner at BURST_ORIGIN.
BURST_PIXELS = (512, 512)
BURST_ORIGIN = (-0.1666, 0.47)
BURST_SIZE = (0.3332, 0.476)


def tex_guitar():
    """A three-tone sunburst: black at the edge, through red, to an amber
    centre with the alder's grain in it. The burst follows the outline, so it
    is painted by distance from the body's edge; the stops are read off the
    photograph the outline came from."""
    w, h = BURST_PIXELS
    rng = np.random.default_rng(13)
    x = BURST_ORIGIN[0] + (np.arange(w) + 0.5) * BURST_SIZE[0] / w
    y = BURST_ORIGIN[1] - (np.arange(h) + 0.5) * BURST_SIZE[1] / h
    X, Y = np.meshgrid(x, y)
    edge = strat_body()
    dist = np.full(X.shape, np.inf)
    inside = np.zeros(X.shape, bool)
    for (ax, ay), (bx, by) in zip(edge, np.roll(edge, -1, 0)):
        ex, ey = bx - ax, by - ay
        t = np.clip(((X - ax) * ex + (Y - ay) * ey) / (ex * ex + ey * ey), 0.0, 1.0)
        dist = np.minimum(dist, np.hypot(X - ax - t * ex, Y - ay - t * ey))
        inside ^= ((ay > Y) != (by > Y)) & (X < ax + (Y - ay) * ex / (ey if ey else 1e-12))
    d = np.where(inside, dist, 0.0)
    black, red, amber = (np.array(c) for c in ((0.008, 0.005, 0.004), (0.3, 0.004, 0.002), (0.49, 0.094, 0.004)))
    to_red = smoothstep(0.018, 0.042, d)[..., None]
    to_amber = smoothstep(0.036, 0.075, d)[..., None]
    col = (black * (1 - to_red) + red * to_red) * (1 - to_amber) + amber * to_amber
    grain = 0.7 * tile_noise(h, w, 4, 44, rng) + 0.3 * tile_noise(h, w, 12, 130, rng)
    col = col * (1.0 + to_amber * (0.14 * grain[..., None] - 0.07))
    return {"strat_burst": png(to_srgb8(col))}


_TEXTURES: Dict[str, bytes] = {}


def texture(name: str) -> bytes:
    if not _TEXTURES:
        for build in (tex_brick, tex_planks, tex_small, tex_guitar):
            _TEXTURES.update(build())
    return _TEXTURES[name]


# ==========================================================================
# Materials
# ==========================================================================

def emissive(rgb, gain: float):
    return tuple(c * gain for c in rgb)


GLASS = {"glass_green": (0.05, 0.16, 0.05), "glass_amber": (0.3, 0.14, 0.03),
         "glass_clear": (0.3, 0.3, 0.28)}

# name -> base colour (linear), roughness, metallic, and optionally textures
# (with the size in metres one repeat covers), emission (HDR, linear) and sheen.
MATERIALS: Dict[str, dict] = {
    "brick": dict(base=(1, 1, 1), rough=0.9, base_tex="brick_albedo", normal_tex="brick_normal", tile=0.9),
    "planks_deck": dict(base=(1, 1, 1), base_tex="planks_albedo", normal_tex="planks_normal",
                        mr_tex="planks_mr", tile=(1.12, 3.2)),
    "planks_floor": dict(base=(0.55, 0.56, 0.58), base_tex="planks_albedo", normal_tex="planks_normal",
                         mr_tex="planks_mr", tile=(0.88, 3.2)),
    "black_paint": dict(base=(0.012, 0.012, 0.012), rough=0.7),
    "nosing": dict(base=(0.085, 0.064, 0.05), rough=0.45),
    "velour": dict(base=(0.018, 0.016, 0.016), rough=1.0, base_tex="velour", tile=0.5,
                   sheen=(0.06, 0.055, 0.055), double=True),
    "steel_black": dict(base=(0.03, 0.03, 0.03), rough=0.4, metal=0.3),
    "galvanized": dict(base=(0.3, 0.3, 0.32), rough=0.5, metal=0.8),
    "tolex": dict(base=(0.022, 0.022, 0.022), rough=0.55, normal_tex="tolex_normal", tile=0.12),
    "grille": dict(base=(0.036, 0.036, 0.04), rough=0.95, base_tex="weave", tile=0.05),
    "grille_light": dict(base=(0.13, 0.12, 0.1), rough=0.95, base_tex="weave", tile=0.05),
    "piping": dict(base=(0.45, 0.42, 0.35), rough=0.4),
    "panel": dict(base=(0.6, 0.52, 0.38), rough=0.35, metal=0.9),
    "knob": dict(base=(0.02, 0.02, 0.02), rough=0.3),
    "jewel": dict(base=(0.5, 0.02, 0.01), rough=0.1, emission=(4.0, 0.15, 0.05)),
    "chrome": dict(base=(0.62, 0.62, 0.64), rough=0.2, metal=1.0),
    "cymbal": dict(base=(0.74, 0.54, 0.3), rough=0.3, metal=1.0),
    "drumhead": dict(base=(0.5, 0.45, 0.36), rough=0.7),
    "drum_shell": dict(base=(0.03, 0.017, 0.011), rough=0.22),
    "hammered": dict(base=(0.4, 0.3, 0.18), rough=0.32, metal=0.8, normal_tex="hammered_normal", tile=0.2),
    "hoop_wood": dict(base=(0.3, 0.16, 0.07), rough=0.3),
    "port": dict(base=(0.004, 0.004, 0.004), rough=0.9),
    "guitar_top": dict(base=(1, 1, 1), rough=0.12, base_tex="strat_burst", tile=BURST_SIZE),
    "guitar_back": dict(base=(0.01, 0.006, 0.005), rough=0.15),   # the burst's black, round the edge
    "maple": dict(base=(0.66, 0.32, 0.11), rough=0.35),
    "rosewood": dict(base=(0.054, 0.029, 0.024), rough=0.5),
    "parchment": dict(base=(0.74, 0.7, 0.62), rough=0.3),
    "cream": dict(base=(0.72, 0.56, 0.34), rough=0.35),
    "fixture_body": dict(base=(0.025, 0.025, 0.025), rough=0.45),
    "lens": dict(base=(0.02, 0.02, 0.02), rough=0.05),       # lit by the renderer
    "chair_wood": dict(base=(0.075, 0.037, 0.018), rough=0.35),
    "counter": dict(base=(0.09, 0.05, 0.028), rough=0.45),
    "counter_top": dict(base=(0.16, 0.08, 0.04), rough=0.15),
    "brass": dict(base=(0.8, 0.6, 0.3), rough=0.3, metal=1.0),
    "bulb": dict(base=(0.8, 0.8, 0.8), rough=0.2, emission=emissive(WARM, 24.0)),
    "diffuser": dict(base=(0.8, 0.8, 0.8), rough=0.5, emission=emissive(WARM, 9.0)),
    "black": dict(base=(0.018, 0.018, 0.018), rough=0.6),
    "rubber": dict(base=(0.012, 0.012, 0.012), rough=0.8),
    "mic_body": dict(base=(0.08, 0.08, 0.08), rough=0.35, metal=0.6),
    "mic_grille": dict(base=(0.5, 0.5, 0.5), rough=0.45, metal=1.0),
    "display": dict(base=(0.01, 0.01, 0.012), rough=0.08),
}
for _name, _glass in GLASS.items():
    # Lit from behind by the back bar's strip: the glow is the glass's colour.
    MATERIALS[_name] = dict(base=_glass, rough=0.06,
                            emission=tuple(g * w * 0.45 for g, w in zip(_glass, WARM)))


# ==========================================================================
# The container
# ==========================================================================

@dataclass
class Node:
    name: str
    matrix: np.ndarray = field(default_factory=lambda: np.eye(4))
    parts: List[Tuple[Mesh, str]] = field(default_factory=list)
    children: List["Node"] = field(default_factory=list)
    extras: Optional[dict] = None
    light: Optional[dict] = None

    def add(self, mesh: Mesh, material: str) -> "Node":
        self.parts.append((mesh, material))
        return self


class GLB:
    """Just enough of glTF 2.0 to write what this file builds."""

    FLOAT, UINT = 5126, 5125
    ARRAY, ELEMENTS = 34962, 34963

    def __init__(self):
        self.gltf = {
            "asset": {"version": "2.0", "generator": "tupperDMX scripts/build_bar_assets.py"},
            "scene": 0, "scenes": [{"nodes": []}], "nodes": [], "meshes": [], "materials": [],
            "accessors": [], "bufferViews": [], "buffers": [],
        }
        self.bin = bytearray()
        self._materials: Dict[str, int] = {}
        self._images: Dict[str, int] = {}
        self._lights: List[dict] = []
        self._extensions = set()

    def _view(self, data: bytes, target: Optional[int] = None) -> int:
        while len(self.bin) % 4:
            self.bin.append(0)
        view = {"buffer": 0, "byteOffset": len(self.bin), "byteLength": len(data)}
        if target:
            view["target"] = target
        self.bin += data
        self.gltf["bufferViews"].append(view)
        return len(self.gltf["bufferViews"]) - 1

    def _accessor(self, array: np.ndarray, kind: str, bounds: bool = False) -> int:
        if kind == "SCALAR":
            data = np.ascontiguousarray(array, np.uint32).ravel()
            accessor = {"bufferView": self._view(data.tobytes(), self.ELEMENTS),
                        "componentType": self.UINT, "count": int(len(data)), "type": kind}
        else:
            data = np.ascontiguousarray(array, np.float32)
            accessor = {"bufferView": self._view(data.tobytes(), self.ARRAY),
                        "componentType": self.FLOAT, "count": int(len(data)), "type": kind}
            if bounds:
                accessor["min"] = data.min(axis=0).tolist()
                accessor["max"] = data.max(axis=0).tolist()
        self.gltf["accessors"].append(accessor)
        return len(self.gltf["accessors"]) - 1

    def _texture(self, name: str) -> int:
        if name not in self._images:
            self.gltf.setdefault("images", []).append(
                {"name": name, "mimeType": "image/png", "bufferView": self._view(texture(name))})
            self.gltf.setdefault("samplers", [{"magFilter": 9729, "minFilter": 9987,
                                               "wrapS": 10497, "wrapT": 10497}])
            self.gltf.setdefault("textures", []).append(
                {"sampler": 0, "source": len(self.gltf["images"]) - 1})
            self._images[name] = len(self.gltf["textures"]) - 1
        return self._images[name]

    def material(self, name: str) -> int:
        if name in self._materials:
            return self._materials[name]
        spec = MATERIALS[name]
        pbr = {"baseColorFactor": list(spec["base"]) + [1.0],
               "metallicFactor": float(spec.get("metal", 0.0)),
               "roughnessFactor": float(spec.get("rough", 0.5))}
        material = {"name": name, "pbrMetallicRoughness": pbr}
        if "base_tex" in spec:
            pbr["baseColorTexture"] = {"index": self._texture(spec["base_tex"])}
        if "mr_tex" in spec:
            pbr["metallicRoughnessTexture"] = {"index": self._texture(spec["mr_tex"])}
            pbr["roughnessFactor"] = 1.0
        if "normal_tex" in spec:
            material["normalTexture"] = {"index": self._texture(spec["normal_tex"])}
        if "emission" in spec:
            peak = max(spec["emission"])
            material["emissiveFactor"] = [c / peak for c in spec["emission"]]
            if peak > 1.0:
                material.setdefault("extensions", {})["KHR_materials_emissive_strength"] = {
                    "emissiveStrength": peak}
                self._extensions.add("KHR_materials_emissive_strength")
        if "sheen" in spec:
            material.setdefault("extensions", {})["KHR_materials_sheen"] = {
                "sheenColorFactor": list(spec["sheen"]), "sheenRoughnessFactor": 0.5}
            self._extensions.add("KHR_materials_sheen")
        if spec.get("double"):
            material["doubleSided"] = True
        self.gltf["materials"].append(material)
        self._materials[name] = len(self.gltf["materials"]) - 1
        return self._materials[name]

    def _mesh(self, name: str, parts: List[Tuple[Mesh, str]]) -> int:
        grouped: Dict[str, List[Mesh]] = {}
        for mesh, material in parts:
            grouped.setdefault(material, []).append(mesh)
        primitives = []
        for material, meshes in grouped.items():
            mesh = merge(meshes)
            spec = MATERIALS[material]
            attributes = {
                "POSITION": self._accessor(mesh.p, "VEC3", bounds=True),
                "NORMAL": self._accessor(mesh.n, "VEC3"),
            }
            if any(key in spec for key in ("base_tex", "normal_tex", "mr_tex")):
                tile = np.broadcast_to(np.asarray(spec.get("tile", 1.0), float), (2,))
                attributes["TEXCOORD_0"] = self._accessor(mesh.uv / tile, "VEC2")
            if mesh.c is not None:
                attributes["COLOR_0"] = self._accessor(mesh.c, "VEC4")
            primitives.append({"attributes": attributes, "indices": self._accessor(mesh.i, "SCALAR"),
                               "material": self.material(material), "mode": 4})
        self.gltf["meshes"].append({"name": name, "primitives": primitives})
        return len(self.gltf["meshes"]) - 1

    def _node(self, node: Node) -> int:
        record: dict = {"name": node.name}
        index = len(self.gltf["nodes"])
        self.gltf["nodes"].append(record)
        if not np.allclose(node.matrix, np.eye(4)):
            record["matrix"] = node.matrix.T.ravel().tolist()       # glTF is column-major
        if node.parts:
            record["mesh"] = self._mesh(node.name, node.parts)
        if node.extras:
            record["extras"] = node.extras
        if node.light:
            self._lights.append(node.light)
            record["extensions"] = {"KHR_lights_punctual": {"light": len(self._lights) - 1}}
            self._extensions.add("KHR_lights_punctual")
        if node.children:
            record["children"] = [self._node(child) for child in node.children]
        return index

    def write(self, path: str, roots: Sequence[Node], extras: Optional[dict] = None) -> int:
        self.gltf["scenes"][0]["nodes"] = [self._node(root) for root in roots]
        if extras:
            self.gltf["scenes"][0]["extras"] = extras
        if self._lights:
            self.gltf["extensions"] = {"KHR_lights_punctual": {"lights": self._lights}}
        if self._extensions:
            self.gltf["extensionsUsed"] = sorted(self._extensions)
        while len(self.bin) % 4:
            self.bin.append(0)
        self.gltf["buffers"] = [{"byteLength": len(self.bin)}]
        text = json.dumps(self.gltf, separators=(",", ":")).encode()
        text += b" " * (-len(text) % 4)
        total = 12 + 8 + len(text) + 8 + len(self.bin)
        with open(path, "wb") as fh:
            fh.write(struct.pack("<III", 0x46546C67, 2, total))
            fh.write(struct.pack("<II", len(text), 0x4E4F534A) + text)
            fh.write(struct.pack("<II", len(self.bin), 0x004E4942) + bytes(self.bin))
        return total


def spot_light(name: str, position, direction, intensity: float, inner_deg: float,
               outer_deg: float, lens_radius: float, shadows: bool = True) -> Node:
    """A KHR_lights_punctual spot. glTF spots shine down their node's -Z."""
    matrix = T(*position) @ align((0.0, 0.0, -1.0), direction)
    return Node(name, matrix, extras={"lens_radius": lens_radius, "cast_shadows": shadows},
                light={"type": "spot", "name": name, "color": list(WARM), "intensity": intensity,
                       "spot": {"innerConeAngle": math.radians(inner_deg),
                                "outerConeAngle": math.radians(outer_deg)}})


# ==========================================================================
# The venue
# ==========================================================================

def curtain_fold(x):
    return 0.045 * np.sin(x * 18.0 + 1.1 * np.sin(x * 2.3)) + 0.012 * np.sin(x * 43.0 + 1.7)


def curtain_fold_dx(x):
    inner = x * 18.0 + 1.1 * np.sin(x * 2.3)
    return (0.045 * np.cos(inner) * (18.0 + 1.1 * 2.3 * np.cos(x * 2.3))
            + 0.012 * 43.0 * np.cos(x * 43.0 + 1.7))


def drape(z_of, dz_dx, bottom_of, top: float, nx: int, ny: int) -> Mesh:
    """A hanging sheet: z(x) folds, bottom(x) hem, straight top. The folds'
    recesses are darkened in COLOR_0 — the one place cavity is baked in."""
    x = np.linspace(-HALF_WIDTH, HALF_WIDTH, nx)
    s = np.linspace(0.0, 1.0, ny)
    X, Sv = np.meshgrid(x, s, indexing="ij")
    bottom = bottom_of(X)
    Y = bottom + (top - bottom) * Sv
    flare = 1.0 + 0.2 * (1.0 - Sv)                  # folds open out toward the hem
    offset = z_of(X)
    Z = offset * flare
    dzdx = dz_dx(X) * flare
    dzdy = -0.2 * offset / max(top - bottom.mean(), 1e-3)
    pos = np.stack([X, Y, Z], -1).reshape(-1, 3)
    nrm = np.stack([-dzdx, -dzdy, np.ones_like(X)], -1).reshape(-1, 3)
    depth = (offset - offset.min()) / max(np.ptp(offset), 1e-6)
    shade = (0.5 + 0.5 * depth).reshape(-1, 1)
    colors = np.concatenate([np.repeat(shade, 3, 1), np.ones((len(pos), 1))], 1)
    uv = np.stack([X, -Y], -1).reshape(-1, 2)
    return oriented(Mesh(pos, nrm, uv, colors, grid_indices(nx, ny)))


def wall_grime(p):
    """Smoke-darkened toward the ceiling, and never evenly."""
    along = p[:, 2] + p[:, 0]
    patchy = 0.7 + 0.3 * hash_noise(along * 0.9, p[:, 1] * 0.9)
    tone = (1.0 - 0.5 * smoothstep(1.0, CEILING_Y, p[:, 1])) * patchy
    return np.repeat(tone[:, None], 3, 1)


def build_venue() -> Tuple[List[Node], dict]:
    W, F, C = HALF_WIDTH, -DECK_HEIGHT, CEILING_Y
    shell = Node("shell", extras={"cast_shadows": False})
    depth = REAR_WALL_Z - BACK_WALL_Z
    # Brick side walls and rear wall; UVs run along the wall, V downward.
    left = plane((-W, C, BACK_WALL_Z), (0, 0, depth), (0, F - C, 0), 60, 16, (1, 0, 0))
    right = plane((W, C, REAR_WALL_Z), (0, 0, -depth), (0, F - C, 0), 60, 16, (-1, 0, 0))
    rear = plane((-W, C, REAR_WALL_Z), (2 * W, 0, 0), (0, F - C, 0), 30, 16, (0, 0, -1))
    for wall in (left, right, rear):
        shell.add(wall.painted(wall_grime), "brick")
    shell.add(plane((-W, C, BACK_WALL_Z), (2 * W, 0, 0), (0, F - C, 0), 1, 1, (0, 0, 1)), "black_paint")
    shell.add(plane((-W, C, BACK_WALL_Z), (2 * W, 0, 0), (0, 0, depth), 1, 1, (0, -1, 0)), "black_paint")
    # Floorboards: the audience floor, and the deck a riser above it.
    shell.add(plane((-W, F, 0.0), (2 * W, 0, 0), (0, 0, REAR_WALL_Z), 1, 1, (0, 1, 0)), "planks_floor")
    shell.add(plane((-W, 0.0, BACK_WALL_Z), (2 * W, 0, 0), (0, 0, -BACK_WALL_Z), 1, 1, (0, 1, 0)),
              "planks_deck")
    shell.add(plane((-W, F, 0.0), (2 * W, 0, 0), (0, -F - 0.035, 0), 1, 1, (0, 0, 1)), "black_paint")
    shell.add(rounded_box((W, 0.02, 0.02), 0.008, center=(0.0, -0.02, 0.0)), "nosing")
    # The drape and the border hung in front of its track.
    shell.add(drape(curtain_fold, curtain_fold_dx, lambda x: np.zeros_like(x), CURTAIN_TOP, 420, 14)
              .transform(T(0, 0, CURTAIN_Z)), "velour")
    border_z = CURTAIN_Z + 0.2
    shell.add(drape(lambda x: 0.03 * np.sin(x * 26.0 + 0.7 * np.sin(x * 3.1)),
                    lambda x: 0.03 * np.cos(x * 26.0 + 0.7 * np.sin(x * 3.1)) * (26.0 + 0.7 * 3.1 * np.cos(x * 3.1)),
                    lambda x: CURTAIN_TOP - 0.34 + 0.012 * np.sin(x * 26.0), C, 320, 6)
              .transform(T(0, 0, border_z)), "velour")

    rigging = Node("rigging")
    for y, z in (FRONT_PIPE, BACK_PIPE, FOH_PIPE):
        rigging.add(rod((-W, y, z), (W, y, z), PIPE_RADIUS, 16), "steel_black")
    for y, z in (FRONT_PIPE, FOH_PIPE):
        for x in (-2.2, 2.2):
            rigging.add(rod((x, y, z), (x, C, z), 0.012, 10), "steel_black")
            rigging.add(rounded_box((0.03, 0.03, 0.04), 0.006, center=(x, y + 0.02, z)), "steel_black")
            rigging.add(disc(0.05, 16, up=False).transform(T(x, C - 0.001, z)), "steel_black")
    for x in (-W + 0.025, W - 0.025):
        rigging.add(rod((x, 0.25, -0.7), (x, C, -0.7), 0.012, 10), "galvanized")
        rigging.add(rounded_box((0.03, 0.06, 0.05), 0.005, center=(x - np.sign(x) * 0.005, 0.2, -0.7)),
                    "galvanized")

    house = Node("house_lights")
    for side, x in (("left", -HOUSE_X), ("right", HOUSE_X)):
        can = Node(f"downlight_{side}", T(x, C, HOUSE_Z))
        can.add(lathe([[(0.058, -0.012), (0.075, -0.012), (0.08, -0.004), (0.08, 0.0)]], 32), "black")
        can.add(lathe([[(0.058, 0.0), (0.058, -0.012)]], 32), "black")
        can.add(disc(0.058, 32, up=False).transform(T(0, -0.006, 0)), "diffuser")
        house.children.append(can)
        house.children.append(spot_light(f"downlight_{side}_lamp", (x, C - 0.08, HOUSE_Z), (0, -1, 0),
                                         HOUSE_OUTPUT, 15.0, 55.0, 0.05))

    extras = {"venue": {
        "units": "metres; +X audience right, +Y up, +Z toward the audience; "
                 "origin at the deck's front edge, deck height",
        "half_width": HALF_WIDTH, "deck_height": DECK_HEIGHT, "ceiling": CEILING_Y,
        "back_wall": BACK_WALL_Z, "curtain_z": CURTAIN_Z, "curtain_top": CURTAIN_TOP,
        "rear_wall": REAR_WALL_Z, "pipe_radius": PIPE_RADIUS,
        "pipes": {"front": list(FRONT_PIPE), "back": list(BACK_PIPE), "foh": list(FOH_PIPE)},
    }}
    return [shell, rigging, house], extras


# ==========================================================================
# The backline
# ==========================================================================

def cabinet(half, grille_margin: float, grille: str = "grille") -> Node:
    """A tolex-covered speaker box standing on the floor: grille on its front,
    piping round the grille, steel corners, rubber feet."""
    hx, hy, hz = half
    lift = 0.02
    node = Node("cabinet")
    node.add(rounded_box(half, 0.018, center=(0, lift + hy, 0)), "tolex")
    gx, gy = hx - grille_margin, hy - grille_margin
    node.add(rounded_box((gx, gy, 0.004), 0.002, center=(0, lift + hy, hz)), grille)
    frame = [(-gx, -gy, 0), (gx, -gy, 0), (gx, gy, 0), (-gx, gy, 0)]
    node.add(tube(frame, 0.004, 8, closed=True).transform(T(0, lift + hy, hz + 0.002)), "piping")
    for sx in (-1, 1):
        for sy in (-1, 1):
            for sz in (-1, 1):
                node.add(rounded_box((0.032, 0.032, 0.032), 0.01,
                                     center=(sx * (hx - 0.014), lift + hy + sy * (hy - 0.014),
                                             sz * (hz - 0.014))), "chrome")
            node.add(cylinder(0.022, lift / 2, 12, 0.004)
                     .transform(T(sx * (hx - 0.06), lift / 2, sy * (hz - 0.05))), "rubber")
    return node


def knob(radius: float) -> Mesh:
    """A chicken-head-ish control knob, axis +Z, sitting on the panel."""
    k = lathe([[(radius * 1.25, 0.0), (radius * 1.25, 0.003), (radius, 0.005),
                (radius * 0.85, radius * 1.4), (0.0, radius * 1.45)]], 20)
    return k.transform(Rx(math.pi / 2))


def build_amp_stack() -> List[Node]:
    root = Node("amp_stack")
    cab = cabinet((0.38, 0.38, 0.18), 0.05)
    root.children.append(cab)
    head = Node("amp_head", T(0, 0.78 + 0.145, -0.04))
    head.add(rounded_box((0.36, 0.145, 0.13), 0.015), "tolex")
    head.add(rounded_box((0.33, 0.055, 0.003), 0.002, center=(0, 0.05, 0.13)), "panel")
    head.add(rounded_box((0.33, 0.05, 0.003), 0.002, center=(0, -0.065, 0.13)), "grille")
    for k in range(9):
        head.add(knob(0.012).transform(T(-0.25 + 0.055 * k, 0.045, 0.133)), "knob")
    head.add(sphere(0.009, 16).transform(T(0.315, 0.045, 0.134)), "jewel")
    handle = [(-0.13, 0.145, 0.0), (-0.12, 0.175, 0.0), (0.0, 0.185, 0.0), (0.12, 0.175, 0.0),
              (0.13, 0.145, 0.0)]
    head.add(tube(handle, 0.011, 10, smooth=6), "black")
    root.children.append(head)
    return [root]


def build_combo() -> List[Node]:
    root = Node("combo")
    hx, hy, hz = 0.31, 0.27, 0.13
    lift = 0.02
    root.add(rounded_box((hx, hy, hz), 0.015, center=(0, lift + hy, 0)), "tolex")
    root.add(rounded_box((hx - 0.035, hy - 0.075, 0.004), 0.002,
                         center=(0, lift + hy - 0.035, hz)), "grille_light")
    frame = [(-(hx - 0.035), -(hy - 0.075), 0), (hx - 0.035, -(hy - 0.075), 0),
             (hx - 0.035, hy - 0.075, 0), (-(hx - 0.035), hy - 0.075, 0)]
    root.add(tube(frame, 0.004, 8, closed=True).transform(T(0, lift + hy - 0.035, hz + 0.002)), "piping")
    root.add(rounded_box((hx - 0.03, 0.022, 0.003), 0.002, center=(0, lift + 2 * hy - 0.035, hz)), "panel")
    for k in range(9):
        root.add(knob(0.009).transform(T(-0.2 + 0.05 * k, lift + 2 * hy - 0.035, hz + 0.003)), "knob")
    handle = [(-0.11, lift + 2 * hy, 0), (-0.1, lift + 2 * hy + 0.028, 0), (0.0, lift + 2 * hy + 0.036, 0),
              (0.1, lift + 2 * hy + 0.028, 0), (0.11, lift + 2 * hy, 0)]
    root.add(tube(handle, 0.011, 10, smooth=6), "black")
    for sx in (-1, 1):
        for sz in (-1, 1):
            root.add(cylinder(0.02, lift / 2, 12, 0.004)
                     .transform(T(sx * (hx - 0.05), lift / 2, sz * (hz - 0.04))), "rubber")
    return [root]


def build_bass_cab() -> List[Node]:
    root = cabinet((0.33, 0.47, 0.22), 0.05)
    root.name = "bass_cab"
    root.add(rounded_box((0.05, 0.012, 0.003), 0.003, center=(0, 0.02 + 0.94 - 0.03, 0.225)), "chrome")
    return [root]


# The guitar is a Stratocaster — HSS, three-tone sunburst, rosewood board —
# traced off a product photograph at 0.837 mm a pixel, the scale at which its
# frets fall for a 648 mm scale length. Its own frame: upright, face +Z, +X to
# the treble side, origin on the bottom edge of the body. Outlines are spline
# control points in millimetres.
STRAT_BODY = [
    (-1.8, 0.8), (15.1, 1.7), (43.9, 1.9), (67.5, 3.6), (91.9, 8.5), (114.1, 17.3), (133.2, 30.9),
    (146.5, 46.9), (156.4, 67.1), (161.3, 90.7), (161.0, 115.8), (156.9, 140.2), (149.3, 166.0),
    (140.9, 186.9), (129.9, 210.2), (119.7, 231.9), (114.0, 253.8), (115.5, 277.0), (123.8, 298.9),
    (131.9, 320.1), (137.4, 344.8), (137.4, 369.3), (131.1, 389.7), (120.4, 399.9), (108.4, 400.5),
    (100.2, 392.8), (95.3, 373.5), (87.2, 355.6), (72.2, 344.6), (52.8, 341.6), (35.0, 344.9), (26.1, 344.2),
    (8.3, 355.7), (-13.1, 369.4), (-25.0, 378.5), (-36.0, 386.2), (-45.2, 387.6), (-64.4, 390.9),
    (-79.4, 401.8), (-89.5, 418.5), (-92.9, 440.3), (-96.3, 458.0), (-99.8, 463.2), (-105.0, 465.6),
    (-111.0, 465.4), (-117.2, 462.4), (-122.5, 458.0), (-128.1, 451.2), (-136.5, 431.1), (-138.9, 409.3),
    (-137.4, 383.2), (-132.3, 359.9), (-124.9, 332.9), (-117.0, 307.3), (-113.0, 282.9), (-115.1, 259.0),
    (-122.6, 236.4), (-131.9, 214.7), (-141.2, 193.1), (-149.7, 171.5), (-156.8, 147.6), (-161.2, 123.5),
    (-161.8, 101.0), (-158.7, 78.2), (-149.2, 57.5), (-134.1, 39.2), (-113.8, 24.3), (-92.0, 13.5),
    (-66.8, 6.9), (-42.2, 3.8), (-17.6, 2.5),
]
STRAT_GUARD = [
    (137.5, 101.1), (146.9, 108.4), (148.0, 125.4), (141.2, 151.2), (132.1, 174.8), (121.6, 198.6),
    (110.5, 223.5), (103.4, 247.3), (103.4, 271.6), (110.1, 295.0), (119.5, 318.3), (126.3, 344.1),
    (126.3, 368.2), (118.4, 387.9), (114.4, 389.8), (110.6, 384.9), (105.7, 362.2), (94.8, 343.8),
    (88.6, 340.9), (81.7, 341.7), (62.5, 335.1), (59.0, 330.1), (52.9, 329.1), (43.9, 335.2), (32.7, 338.1),
    (21.4, 340.3), (-1.3, 355.2), (-23.6, 369.7), (-30.9, 369.9), (-36.7, 365.9), (-47.1, 367.6),
    (-54.2, 362.2), (-52.7, 347.4), (-48.1, 320.2), (-48.1, 293.3), (-54.0, 266.6), (-62.6, 243.4),
    (-71.3, 219.5), (-75.3, 194.2), (-72.3, 171.3), (-62.4, 154.4), (-47.6, 146.6), (-41.4, 147.8),
    (-39.5, 158.4), (-35.0, 161.3), (-5.0, 161.5), (28.5, 161.5), (45.6, 160.3), (47.9, 150.9),
    (50.9, 146.3), (72.5, 142.8), (91.0, 133.7), (111.4, 117.2), (126.8, 103.3),
]
STRAT_HEAD = [
    (21.2, 803.2), (22.0, 811.0), (24.0, 820.6), (31.2, 832.6), (39.9, 839.8), (46.4, 844.1), (46.1, 856.4),
    (44.3, 868.7), (40.4, 884.1), (37.0, 899.1), (34.2, 915.6), (34.4, 924.9), (42.6, 931.7), (52.5, 941.9),
    (55.2, 953.3), (53.0, 968.6), (46.0, 976.8), (37.5, 981.6), (24.5, 981.6), (14.0, 976.1), (6.7, 964.2),
    (3.2, 952.6), (-2.7, 932.0), (-12.0, 900.0), (-21.3, 868.0), (-30.5, 836.5), (-34.2, 822.7),
    (-31.6, 815.4), (-23.0, 809.0), (-21.5, 803.4), (-19.8, 800.8), (-16.8, 799.4), (-12.0, 799.0),
    (-6.0, 799.0), (0.0, 799.0), (6.0, 799.0), (12.0, 799.0), (16.8, 799.4), (19.8, 800.8),
]
GUARD_SCREWS = [(-45.6, 358.4), (115.7, 375.2), (111.6, 315.5), (35.6, 329.8), (-50.1, 257.0),
                (-66.4, 205.7), (-51.2, 153.3), (102.7, 220.4), (124.9, 169.8), (136.1, 111.3)]
KNOBS = [(54.8, 177.4), (92.1, 157.4), (124.3, 126.4)]            # volume, tone, tone
SCALE_LENGTH = 0.648
NUT_Y = 0.798
BODY_HALF = 0.0225                                                # 45 mm of alder
STRING_RADII = (0.0006, 0.0005, 0.0004, 0.00032, 0.00026, 0.00022)  # low E to high E


def strat_body() -> np.ndarray:
    return outline_mm(STRAT_BODY)


def strat_knob() -> Mesh:
    """A skirted, flat-topped control knob standing on +Z."""
    r = 0.0118
    knob = lathe([[(r, 0.0), (r, 0.0016)], [(r, 0.0016), (0.011, 0.0022)], [(0.011, 0.0022), (0.0101, 0.0158)],
                  [(0.0101, 0.0158), (0.0097, 0.0166), (0.0092, 0.017), (0.0, 0.0172)]], 28)
    return knob.transform(Rx(math.pi / 2))


def screw_head(radius: float = 0.0021) -> Mesh:
    """A domed screw head standing on +Z."""
    return lathe([[(radius, 0.0), (radius * 0.96, radius * 0.25), (radius * 0.6, radius * 0.5),
                   (0.0, radius * 0.55)]], 12).transform(Rx(math.pi / 2))


def strap_button() -> Mesh:
    """A strap button standing on +Y."""
    return lathe([[(0.0036, 0.0), (0.0036, 0.0055)],
                  [(0.0036, 0.0055), (0.0066, 0.0062), (0.0068, 0.0084), (0.0055, 0.0104), (0.0, 0.011)]], 16)


def c_neck(y0: float, y1: float, w0: float, w1: float, d0: float, d1: float, top: float,
           stations: int = 16, seg: int = 18) -> Mesh:
    """A neck's back: at each station along Y a half-ellipse hanging from the
    plane z = top, its width and depth running linearly from (w0, d0) at y0 to
    (w1, d1) at y1; capped flat at y0, where the neck ends in the body."""
    s = np.linspace(0.0, 1.0, stations)[:, None]
    t = np.linspace(0.0, np.pi, seg + 1)[None, :]
    a = (w0 + (w1 - w0) * s) / 2
    b = d0 + (d1 - d0) * s
    y = np.broadcast_to(y0 + (y1 - y0) * s, (stations, seg + 1))
    pos = np.stack([a * np.cos(t), y, top - b * np.sin(t)], -1).reshape(-1, 3)
    nrm = np.stack([np.cos(t) / a, np.zeros_like(y), -np.sin(t) / b], -1).reshape(-1, 3)
    back = Mesh(pos, nrm, None, None, grid_indices(stations, seg + 1))
    heel = Mesh(np.concatenate([[(0.0, y0, top)], pos[:seg + 1]]), np.tile([0.0, -1.0, 0.0], (seg + 2, 1)),
                None, None, [[0, k + 1, k + 2] for k in range(seg)])
    return oriented(merge([back, heel]))


def build_guitar() -> List[Node]:
    root = Node("guitar")
    # The stand, in the asset's frame: an A-frame the guitar leans back into.
    stand = [rod((0, 0.02, -0.3), (0, 0.8, -0.18), 0.009)]
    for sx in (-1, 1):
        stand.append(rod((sx * 0.18, 0.012, 0.08), (0, 0.05, -0.3), 0.009))
        stand.append(rod((sx * 0.13, 0.13, 0.07), (0, 0.1, -0.12), 0.008))
        stand.append(rod((sx * 0.13, 0.13, 0.07), (sx * 0.13, 0.17, 0.08), 0.011))
        stand.append(rod((sx * 0.05, 0.84, -0.16), (0, 0.8, -0.18), 0.007))
    root.add(merge(stand), "black")
    # The guitar in its own frame, tipped back onto the stand: the lower bout
    # on the cradle arms, the neck in the yoke.
    body = Node("guitar_body", T(-0.004, 0.1255, 0.02) @ Rx(-0.29))
    face = Rx(math.pi / 2)                          # a lathe's +Y, turned to face +Z
    top_z = BODY_HALF
    guard_top = top_z + 0.0023                      # three-ply, 2.3 mm
    plate_top = top_z + 0.0018                      # the bridge's base plate
    maple_top = top_z + 0.0035                      # the neck's face, under the board
    board_top = maple_top + 0.005
    nut_z, saddle_z = board_top + 0.0018, board_top + 0.005     # string centres
    saddle_y = NUT_Y - SCALE_LENGTH
    chrome, cream, parchment, black = [], [], [], []

    # The body: rounded over, the burst on both faces, its black round the edge.
    edge = strat_body()
    front, back, sides = rounded_slab(edge, BODY_HALF, 0.006)
    for cap in (front, back):
        uv = np.stack([cap.p[:, 0] - BURST_ORIGIN[0], BURST_ORIGIN[1] - cap.p[:, 1]], 1)
        body.add(Mesh(cap.p, cap.n, uv, None, cap.i), "guitar_top")
    body.add(sides, "guitar_back")
    for k in (int(np.argmax(edge[:, 1])), int(np.argmin(edge[:, 1]))):      # horn tip and tail
        t = edge[(k + 1) % len(edge)] - edge[k - 1]
        chrome.append(strap_button().transform(T(*edge[k], 0.0) @ align((0, 1, 0), (t[1], -t[0], 0.0))))

    # The pickguard, its bevel showing the black middle ply, and its screws.
    guard, _, guard_edge = rounded_slab(outline_mm(STRAT_GUARD), 0.00115, 0.0009)
    parchment.append(guard.transform(T(0, 0, top_z + 0.00115)))
    black.append(guard_edge.transform(T(0, 0, top_z + 0.00115)))
    chrome += [screw_head().transform(T(x / 1000, y / 1000, guard_top)) for x, y in GUARD_SCREWS]

    # Pickups: single coils at the neck and middle, a humbucker at the bridge.
    cover = 0.0034
    single = rounded_slab(rounded_rect(0.0342, 0.0091, 0.0065), cover, 0.0024)
    bobbin = rounded_slab(rounded_rect(0.0325, 0.0085, 0.0045), cover, 0.0022)
    pole = lathe([[(0.0024, 0.0), (0.0024, 0.0003)], [(0.0024, 0.0003), (0.0016, 0.0007), (0.0, 0.0008)]],
                 10).transform(face)
    for y in (0.307, 0.2465):
        cream += [m.transform(T(0, y, guard_top + cover)) for m in (single[0], single[2])]
        chrome += [pole.transform(T((k - 2.5) * 0.0104, y, guard_top + 2 * cover - 0.0003)) for k in range(6)]
        chrome += [screw_head(0.0018).transform(T(sx * 0.038, y, guard_top)) for sx in (-1, 1)]
    hb_y = 0.1938
    ring = rounded_slab(rounded_rect(0.035, 0.0192, 0.005), 0.0008, 0.0005)
    black += [m.transform(T(0, hb_y, guard_top + 0.0008)) for m in (ring[0], ring[2])]
    for y in (hb_y - 0.0087, hb_y + 0.0087):
        cream += [m.transform(T(0, y, guard_top + cover)) for m in (bobbin[0], bobbin[2])]
        chrome += [pole.transform(T((k - 2.5) * 0.0097, y, guard_top + 2 * cover - 0.0003)) for k in range(6)]
    chrome += [screw_head(0.0018).transform(T(x, y, guard_top))
               for x, y in ((-0.0402, 0.2), (-0.0402, 0.1883), (0.0385, 0.1942))]

    # Controls: three knobs, the five-way switch in its slot, the jack's cup.
    cream += [strat_knob().transform(T(x / 1000, y / 1000, guard_top)) for x, y in KNOBS]
    switch = T(0.0957, 0.2023, guard_top) @ Rz(math.radians(-49.0))
    black.append(rounded_box((0.0115, 0.0016, 0.0002), 0.0002).transform(switch))
    cream.append(rounded_box((0.003, 0.0045, 0.005), 0.0025, center=(0.003, 0.0, 0.005)).transform(switch))
    chrome += [screw_head(0.0018).transform(switch @ T(sx * 0.0215, 0, 0)) for sx in (-1, 1)]
    jack = T(0.0942, 0.0873, top_z) @ Rz(math.radians(-36.6))
    a = np.linspace(0.0, 2 * np.pi, 40, endpoint=False)
    cup = rounded_slab(np.stack([0.0405 * np.cos(a), 0.0158 * np.sin(a)], 1), 0.0007, 0.0005)
    chrome += [m.transform(jack @ T(0, 0, 0.0007)) for m in (cup[0], cup[2])]
    chrome.append(cylinder(0.0062, 0.002, 20, 0.0007).transform(jack @ T(0, 0, 0.0034) @ face))
    black.append(disc(0.0036, 16).transform(jack @ T(0, 0, 0.0055) @ face))
    chrome += [screw_head(0.0018).transform(jack @ T(sx * 0.0345, 0, 0.0014)) for sx in (-1, 1)]

    # The tremolo: base plate, its turned-up back edge, six saddles, the arm.
    plate = rounded_slab(rounded_rect(0.038, 0.0214, 0.004), 0.0009, 0.0006)
    chrome += [m.transform(T(0, 0.1385, top_z + 0.0009)) for m in (plate[0], plate[2])]
    chrome.append(rounded_box((0.037, 0.0012, 0.0045), 0.001, center=(0, 0.1183, plate_top + 0.0045)))
    for i, r in enumerate(STRING_RADII):
        height = saddle_z - r - plate_top
        chrome.append(rounded_box((0.0047, 0.014, height / 2), 0.0015,
                                  center=((i - 2.5) * 0.0105, saddle_y - 0.014, plate_top + height / 2)))
    chrome += [screw_head().transform(T(sx * 0.0293, 0.1567, plate_top)) for sx in (-1, 1)]
    arm = [(0.035, 0.1281, plate_top - 0.002), (0.0352, 0.1286, plate_top + 0.008),
           (0.037, 0.1335, plate_top + 0.016), (0.046, 0.155, plate_top + 0.0215),
           (0.0572, 0.1811, plate_top + 0.023), (0.06, 0.2313, plate_top + 0.0235)]
    chrome.append(tube(arm, 0.0024, 10, smooth=5))
    tip_end = (0.0608, 0.2676, plate_top + 0.0235)
    cream.append(tube([arm[-1], tip_end], 0.0033, 12))
    cream.append(sphere(0.0033, 12).transform(T(*tip_end)))

    # The neck: a C-shaped maple back under a rosewood board, 22 frets, dots.
    board_end = 0.3215
    widths = (0.0557, 0.0427)                       # at the board's end, at the nut
    body.add(c_neck(board_end, NUT_Y, widths[0], widths[1], 0.0185, 0.0145, maple_top), "maple")
    board = rounded_box((0.025, (NUT_Y - board_end) / 2, 0.0025), 0.0008,
                        center=(0, (NUT_Y + board_end) / 2, maple_top + 0.0025))
    body.add(tapered(board, board_end, NUT_Y, widths[0] / 0.05, widths[1] / 0.05), "rosewood")

    def fret_y(n: int) -> float:
        return NUT_Y - SCALE_LENGTH * (1.0 - 2.0 ** (-n / 12.0))

    for n in range(1, 23):
        half = float(np.interp(fret_y(n), [board_end, NUT_Y], widths)) / 2 - 0.0004
        chrome.append(rod((-half, fret_y(n), board_top), (half, fret_y(n), board_top), 0.0011, 8))
    for n in (3, 5, 7, 9, 12, 15, 17, 19, 21):
        y = (fret_y(n - 1) + fret_y(n)) / 2
        cream += [disc(0.003, 16).transform(T(x, y, board_top + 0.0001) @ face)
                  for x in ((-0.0115, 0.0115) if n == 12 else (0.0,))]
    parchment.append(rounded_box((0.0213, 0.0024, 0.0036), 0.0008, center=(0, NUT_Y + 0.0024, maple_top + 0.0036)))

    # The headstock, the truss rod's slot at its root, six tuners in line down
    # the bass edge: bushing and post on the face, the gear box behind, its
    # shaft and button reaching out past the edge.
    head_half = 0.00725
    body.add(merge(rounded_slab(outline_mm(STRAT_HEAD), head_half, 0.0015))
             .transform(T(0, 0, maple_top - head_half)), "maple")
    head_back = maple_top - 2 * head_half
    black.append(rounded_box((0.0026, 0.0064, 0.0003), 0.0003, center=(0, 0.8186, maple_top)))
    reach = np.array([-0.0191, 0.0099])
    turn = Rz(math.atan2(reach[1], reach[0]))
    posts = [np.array([0.0151 - 0.007 * k, 0.9556 - 0.02364 * k]) for k in range(6)]   # high E first
    for px, py in posts:
        bx, by = (px, py) + reach
        chrome.append(cylinder(0.0052, 0.0008, 16, 0.0003).transform(T(px, py, maple_top + 0.0008) @ face))
        chrome.append(cylinder(0.0029, 0.0045, 12, 0.0005).transform(T(px, py, maple_top + 0.0061) @ face))
        chrome.append(rounded_box((0.0085, 0.0075, 0.0055), 0.002)
                      .transform(T(px, py, head_back - 0.0055) @ turn @ T(0.002, 0, 0)))
        chrome.append(rod((px, py, head_back - 0.005), (bx, by, head_back - 0.005), 0.0016, 8))
        parchment.append(sphere(1.0, 16).transform(T(bx, by, head_back - 0.005) @ turn @ S(0.0062, 0.0056, 0.0026)))
    tree_x, tree_y, tree_z = 0.0145, 0.8709, maple_top + 0.0045
    chrome.append(rod((tree_x - 0.006, tree_y, tree_z), (tree_x + 0.006, tree_y, tree_z), 0.0018, 10))
    chrome.append(cylinder(0.0012, 0.0014, 8).transform(T(tree_x, tree_y, maple_top + 0.0014) @ face))

    # Strings: through the plate, over the saddles and the nut to the posts,
    # the B and top E under the string tree on the way.
    for i, r in enumerate(STRING_RADII):
        at_saddle, at_nut = (i - 2.5) * 0.0105, (i - 2.5) * 0.0071
        px, py = posts[5 - i]                       # the low E winds nearest the nut
        path = [(at_saddle, saddle_y - 0.024, plate_top), (at_saddle, saddle_y, saddle_z), (at_nut, NUT_Y, nut_z)]
        if i >= 4:
            x = at_nut + (px - at_nut) * (tree_y - NUT_Y) / (py - NUT_Y)
            path.append((x, tree_y, tree_z - 0.0018 - r))
        path.append((px, py, maple_top + 0.0045))
        chrome.append(tube(path, r, 5))

    for material, parts in (("chrome", chrome), ("cream", cream), ("parchment", parchment), ("black", black)):
        body.add(merge(parts), material)
    root.children.append(body)
    return [root]


def drum(name: str, radius: float, half_depth: float, shell: str, hoop: str, lugs: int,
         matrix: np.ndarray) -> Node:
    """A drum along its local Y: shell, a head each end, a hoop round each
    head, lugs round the shell and a tension rod from each lug to each hoop."""
    r, h = radius, half_depth
    node = Node(name, matrix)
    node.add(lathe([[(r, -h + 0.002), (r, h - 0.002)]], 40), shell)
    head = lathe([[(r - 0.004, 0.0), (r * 0.6, 0.0012), (0.0, 0.0018)]], 40)
    node.add(merge([head.transform(T(0, h, 0)), head.transform(T(0, -h, 0) @ Rx(math.pi))]), "drumhead")
    ring = lathe([[(r + 0.001, -0.014), (r + 0.011, -0.014)], [(r + 0.011, -0.014), (r + 0.011, 0.004)],
                  [(r + 0.011, 0.004), (r - 0.002, 0.004)], [(r - 0.002, 0.004), (r - 0.002, -0.002)]], 40)
    node.add(merge([ring.transform(T(0, h, 0)), ring.transform(T(0, -h, 0) @ Rx(math.pi))]), hoop)
    fittings = []
    for k in range(lugs):
        a = 2 * np.pi * k / lugs
        c, s = math.sin(a), math.cos(a)
        fittings.append(rounded_box((0.009, min(0.025, h * 0.5), 0.007), 0.004)
                        .transform(Ry(a) @ T(0, 0, r + 0.006)))
        for sign in (-1, 1):
            fittings.append(rod((c * (r + 0.008), sign * min(0.028, h * 0.55), s * (r + 0.008)),
                                (c * (r + 0.008), sign * (h + 0.004), s * (r + 0.008)), 0.0025, 6))
    node.add(merge(fittings), "chrome")
    return node


def cymbal_mesh(radius: float) -> Mesh:
    R = radius
    top = [(0.0, 0.021), (0.03, 0.02), (0.055, 0.012), (0.068, 0.0), (0.075, -0.004)]
    top += [(0.075 + (R - 0.075) * t, -0.004 - 0.026 * t) for t in np.linspace(0.1, 1.0, 10)]
    under = [(x, y - 0.0028) for x, y in top]
    # Anticlockwise in (r, y): out along the underside, round the edge, back
    # in along the top.
    return lathe([under, [under[-1], top[-1]], top[::-1]], 48)


def tripod(spread: float, hub: float, radius: float = 0.008, phase: float = 0.0) -> Mesh:
    legs = []
    for k in range(3):
        a = phase + 2 * np.pi * k / 3
        foot = (spread * math.cos(a), 0.012, spread * math.sin(a))
        legs.append(rod(foot, (0.02 * math.cos(a), hub, 0.02 * math.sin(a)), radius, 8))
        legs.append(sphere(0.014, 10).transform(T(foot[0], 0.012, foot[2])))
    return merge(legs)


def stand(spread: float, top: float, phase: float = 0.0) -> Mesh:
    return merge([tripod(spread, 0.25, 0.007, phase), rod((0, 0.2, 0), (0, top, 0), 0.009, 10),
                  cylinder(0.016, 0.02, 12, 0.004).transform(T(0, 0.25, 0))])


def build_drum_kit() -> List[Node]:
    """Origin on the floor under the bass drum's centre; the drummer sits -Z."""
    root = Node("drum_kit")
    kick = drum("kick", 0.275, 0.2, "drum_shell", "hoop_wood", 8, T(0, 0.3, 0) @ Rx(math.pi / 2))
    root.children.append(kick)
    root.add(disc(0.055, 32).transform(T(0.09, 0.2, 0.2045) @ Rx(math.pi / 2)), "port")
    for sx in (-1, 1):
        root.add(rod((sx * 0.25, 0.22, 0.1), (sx * 0.36, 0.008, 0.2), 0.008, 8), "chrome")
    root.add(rounded_box((0.05, 0.01, 0.12), 0.004, center=(0, 0.01, -0.34)), "black")
    root.add(rod((0, 0.03, -0.26), (0, 0.24, -0.22), 0.004, 6), "chrome")
    root.add(sphere(0.022, 12).transform(T(0, 0.26, -0.215)), "black")

    root.children.append(drum("rack_tom", 0.15, 0.1, "drum_shell", "chrome", 6,
                              T(-0.13, 0.76, -0.1) @ Rx(-0.45)))
    root.add(rod((-0.05, 0.57, -0.08), (-0.11, 0.68, -0.1), 0.012, 10), "chrome")

    root.children.append(drum("floor_tom", 0.2, 0.19, "hammered", "chrome", 8, T(0.58, 0.49, -0.4)))
    for k in range(3):
        a = 0.4 + 2 * np.pi * k / 3
        root.add(rod((0.58 + 0.215 * math.cos(a), 0.55, -0.4 + 0.215 * math.sin(a)),
                     (0.58 + 0.3 * math.cos(a), 0.008, -0.4 + 0.3 * math.sin(a)), 0.007, 8), "chrome")

    root.children.append(drum("snare", 0.178, 0.065, "chrome", "chrome", 10,
                              T(-0.46, 0.62, -0.46) @ Rx(-0.12)))
    snare_stand = [stand(0.26, 0.55, 0.9).transform(T(-0.46, 0, -0.46))]
    for k in range(3):
        a = 0.9 + 2 * np.pi * k / 3
        snare_stand.append(rod((-0.46, 0.53, -0.46),
                               (-0.46 + 0.15 * math.cos(a), 0.56, -0.46 + 0.15 * math.sin(a)), 0.005, 6))
    root.add(merge(snare_stand), "chrome")

    root.add(stand(0.28, 0.99, 0.2).transform(T(-0.86, 0, -0.3)), "chrome")
    root.add(rounded_box((0.05, 0.008, 0.13), 0.004, center=(-0.86, 0.01, -0.5)), "black")
    for y in (0.9, 0.96):
        root.children.append(Node("hi_hat", T(-0.86, y, -0.3)).add(cymbal_mesh(0.178), "cymbal"))

    root.add(stand(0.3, 1.18, 1.1).transform(T(-0.62, 0, -0.05)), "chrome")
    root.children.append(Node("crash", T(-0.62, 1.22, -0.05) @ Rx(-0.3) @ Rz(0.15))
                         .add(cymbal_mesh(0.23), "cymbal"))
    root.add(stand(0.3, 1.02, 0.5).transform(T(0.86, 0, -0.18)), "chrome")
    root.children.append(Node("ride", T(0.86, 1.06, -0.18) @ Rx(-0.22) @ Rz(-0.18))
                         .add(cymbal_mesh(0.26), "cymbal"))

    seat = lathe([[(0, -0.035), (0.15, -0.035), (0.165, -0.02), (0.17, 0.0), (0.16, 0.025),
                   (0.12, 0.04), (0.0, 0.045)]], 32)
    root.add(seat.transform(T(0, 0.52, -0.8)), "black")
    root.add(merge([tripod(0.28, 0.18, 0.009).transform(T(0, 0, -0.8)),
                    rod((0, 0.15, -0.8), (0, 0.49, -0.8), 0.014, 10)]), "chrome")
    return [root]


def build_mic_stand() -> List[Node]:
    root = Node("mic_stand")
    root.add(merge([tripod(0.26, 0.16, 0.008, 0.3), rod((0, 0.12, 0), (0, 0.9, 0), 0.011, 12)]), "black")
    root.add(cylinder(0.017, 0.022, 16, 0.004).transform(T(0, 0.92, 0)), "black")
    root.add(rod((0, 0.9, 0), (0, 1.38, 0), 0.008, 12), "chrome")
    mic = Node("mic", T(0, 1.38, 0) @ Rx(0.3))
    mic.add(rounded_box((0.012, 0.02, 0.022), 0.004, center=(0, 0.02, 0)), "black")    # the clip
    mic.add(lathe([[(0.0, -0.01), (0.009, -0.01), (0.012, 0.0), (0.016, 0.1), (0.02, 0.145),
                    (0.021, 0.16)]], 24), "mic_body")
    mic.add(sphere(0.026, 24).transform(T(0, 0.182, 0)), "mic_grille")
    root.children.append(mic)
    # The cable, down the stand and away across the deck to the back line.
    cable = [(0.004, 1.37, -0.012), (0.02, 1.25, -0.01), (0.018, 1.0, 0.0), (0.024, 0.6, 0.012),
             (0.05, 0.2, 0.02), (0.09, 0.01, 0.0), (0.18, 0.006, -0.3), (0.08, 0.006, -0.75),
             (0.25, 0.006, -1.2), (0.2, 0.006, -1.5)]
    root.add(tube(cable, 0.0035, 8, smooth=8), "rubber")
    return [root]


# ==========================================================================
# Furniture
# ==========================================================================

def build_chair() -> List[Node]:
    """A bentwood bistro chair; origin on the floor under the seat, back +Z."""
    root = Node("chair")
    seat = lathe([[(0.0, 0.443), (0.19, 0.443), (0.2, 0.448), (0.203, 0.456), (0.2, 0.464),
                   (0.19, 0.467), (0.0, 0.467)]], 40)
    root.add(seat, "chair_wood")
    wood = []
    for sx in (-1, 1):
        wood.append(tube([(sx * 0.16, 0.0, -0.18), (sx * 0.15, 0.22, -0.155), (sx * 0.14, 0.445, -0.13)],
                         [0.011, 0.012, 0.013], 10, smooth=4))
        wood.append(tube([(sx * 0.16, 0.0, 0.2), (sx * 0.14, 0.44, 0.14), (sx * 0.145, 0.62, 0.17),
                          (sx * 0.15, 0.8, 0.2)], 0.013, 10, caps=True, smooth=6))
    loop = circle_path(0.15, 33, 0.0, np.pi) + np.array([0.0, 0.8, 0.2])
    wood.append(tube(loop, 0.012, 10, caps=False))
    inner = circle_path(0.1, 25, 0.0, np.pi) + np.array([0.0, 0.63, 0.18])
    wood.append(tube(inner, 0.009, 8, caps=False))
    wood.append(rod((-0.146, 0.63, 0.178), (0.146, 0.63, 0.178), 0.009, 8))
    ring = circle_path(0.205, 48, 0.0, 2 * np.pi)[:-1]
    wood.append(tube(ring[:, [0, 2, 1]] + np.array([0.0, 0.2, 0.01]), 0.008, 8, closed=True))
    root.add(merge(wood), "chair_wood")
    return [root]


def bottle_mesh(radius: float, height: float) -> Mesh:
    neck = 0.012
    shoulder = height * 0.62
    profile = [(0.0, 0.0), (radius - 0.004, 0.0), (radius, 0.005), (radius, shoulder - 0.03),
               (radius * 0.75, shoulder), (neck * 1.15, shoulder + 0.04), (neck, height - 0.02),
               (neck * 1.2, height - 0.012), (neck * 1.2, height), (0.0, height)]
    return lathe([profile], 20)


def build_bar() -> List[Node]:
    """The counter, back bar and stools. Origin on the floor at the middle of
    the counter; the counter's front faces the stage (-Z), the rear wall is
    0.95 m behind the origin."""
    root = Node("bar")
    counter = Node("counter")
    counter.add(rounded_box((2.3, 0.52, 0.28), 0.01, center=(0, 0.52, 0)), "counter")
    mouldings = [rounded_box((0.015, 0.43, 0.008), 0.004, center=(-2.2 + 0.4 * k, 0.55, -0.285))
                 for k in range(12)]
    counter.add(merge(mouldings), "counter")
    counter.add(rounded_box((2.3, 0.05, 0.01), 0.003, center=(0, 0.05, -0.284)), "black")
    counter.add(rounded_box((2.38, 0.028, 0.34), 0.012, center=(0, 1.07, -0.03)), "counter_top")
    rail = [rod((-2.28, 0.2, -0.42), (2.28, 0.2, -0.42), 0.02, 14)]
    for x in np.linspace(-2.0, 2.0, 5):
        rail.append(rod((x, 0.2, -0.42), (x, 0.2, -0.28), 0.01, 8))
    counter.add(merge(rail), "brass")
    root.children.append(counter)

    back = Node("back_bar")
    wall = 0.95
    for y in (1.42, 1.84):
        back.add(rounded_box((2.0, 0.015, 0.14), 0.004, center=(0, y - 0.015, wall - 0.14)), "counter_top")
    back.add(rounded_box((2.05, 0.05, 0.13), 0.006, center=(0, 2.26, wall - 0.13)), "counter")
    rng = np.random.default_rng(21)
    bottles: Dict[str, List[Mesh]] = {name: [] for name in GLASS}
    for y in (1.42, 1.84):
        for x in np.arange(-1.87, 1.88, 0.11):
            if rng.random() < 0.12:
                continue
            radius = 0.03 + 0.012 * rng.random()
            height = 0.2 + 0.13 * rng.random()
            glass = list(GLASS)[int(rng.integers(0, 3))]
            bottles[glass].append(bottle_mesh(radius, height).transform(T(x, y, wall - 0.13)))
    for glass, meshes in bottles.items():
        back.add(merge(meshes), glass)
    for x in (-1.0, 1.0):
        back.children.append(spot_light(f"backbar_{'left' if x < 0 else 'right'}_strip",
                                        (x, 2.2, wall - 0.17), (0.0, -1.0, 0.12), BACKBAR_OUTPUT,
                                        30.0, 75.0, 0.5, shadows=False))
    root.children.append(back)

    stools = Node("stools")
    seat = lathe([[(0.0, -0.025), (0.16, -0.025), (0.17, -0.012), (0.172, 0.0), (0.165, 0.018),
                   (0.12, 0.03), (0.0, 0.032)]], 32)
    for x in np.linspace(-1.6, 1.6, 5):
        z = -0.64
        stools.add(seat.transform(T(x, 0.76, z)), "chair_wood")
        frame = [rod((x, 0.02, z), (x, 0.74, z), 0.02, 12),
                 tube((circle_path(0.16, 40)[:-1][:, [0, 2, 1]] + np.array([x, 0.3, z])), 0.009, 8,
                      closed=True),
                 cylinder(0.2, 0.01, 32, 0.004).transform(T(x, 0.01, z))]
        for k in range(3):
            a = 2 * np.pi * k / 3
            frame.append(rod((x, 0.3, z), (x + 0.16 * math.cos(a), 0.3, z + 0.16 * math.sin(a)), 0.006, 6))
        stools.add(merge(frame), "black")
    root.children.append(stools)

    pendants = Node("pendants")
    ceiling = CEILING_Y + DECK_HEIGHT
    lamp_y = 2.05
    for k, x in enumerate((-1.4, 0.0, 1.4)):
        lamp = Node(f"pendant_{k}", T(x, lamp_y, 0.0))
        lamp.add(rod((0, 0.1, 0), (0, ceiling - lamp_y, 0), 0.004, 6), "black")
        lamp.add(disc(0.05, 20, up=False).transform(T(0, ceiling - lamp_y - 0.002, 0)), "black")
        # One sheet: the renderer lights a surface from whichever side it sees.
        lamp.add(lathe([[(0.175, -0.1), (0.035, 0.1)]], 32), "black")
        lamp.add(cylinder(0.04, 0.02, 20, 0.005).transform(T(0, 0.1, 0)), "black")
        lamp.add(sphere(0.045, 20).transform(T(0, -0.02, 0)), "bulb")
        pendants.children.append(lamp)
        pendants.children.append(spot_light(f"pendant_{k}_lamp", (x, lamp_y - 0.06, 0.0), (0, -1, 0),
                                            PENDANT_OUTPUT, 20.0, 62.0, 0.04))
    root.children.append(pendants)
    return [root]


# ==========================================================================
# Fixtures
# ==========================================================================

def clamp_parts() -> List[Mesh]:
    """A hook clamp round the pipe (the node's origin is the pipe's centre)."""
    hook = circle_path(PIPE_RADIUS + 0.01, 24, -0.4 * np.pi, 1.3 * np.pi)
    hook = hook[:, [2, 1, 0]]                      # a ring round X, the pipe's axis
    return [tube(hook, 0.007, 8), rounded_box((0.02, 0.022, 0.028), 0.005, center=(0, -0.05, 0))]


def build_par_can() -> List[Node]:
    """par-rgbw at its definition's size (180 x 200 x 180 mm), hung from a
    pipe: clamp at the origin, a yoke that pans, a body that tilts."""
    r, h = 0.09, 0.1
    drop = 0.3
    arm_h = r + 0.06
    root = Node("par", extras={"dimensions_mm": [180, 200, 180], "lens_radius": round(r * 0.84, 4)})
    root.add(merge(clamp_parts() + [rod((0, -0.07, 0), (0, -(drop - arm_h), 0), 0.008, 10)]), "fixture_body")
    yoke = Node("yoke")
    top = -(drop - arm_h)
    arms = [rounded_box((r + 0.023, 0.006, 0.022), 0.004, center=(0, top, 0))]
    for sx in (-1, 1):
        arms.append(rounded_box((0.005, arm_h / 2 + 0.012, 0.022), 0.003,
                                center=(sx * (r + 0.018), top - arm_h / 2, 0)))
        arms.append(cylinder(0.018, 0.008, 16, 0.003).transform(T(sx * (r + 0.03), -drop, 0) @ Rz(math.pi / 2)))
    yoke.add(merge(arms), "fixture_body")
    body = Node("body", T(0, -drop, 0))
    # The can along its local Y, lens down (-Y): heat-sink fins at the back.
    profile = [[(r * 0.92, -h + 0.005), (r * 0.92, -h - 0.022)],
               [(r * 0.92, -h - 0.022), (r, -h - 0.022)],
               [(r, -h - 0.022), (r, h - 0.072)]]
    for k in range(5):                             # each fin face its own crease
        y0 = h - 0.072 + 0.012 * k
        profile += [[(r, y0), (r * 0.9, y0 + 0.004)], [(r * 0.9, y0 + 0.004), (r * 0.9, y0 + 0.008)],
                    [(r * 0.9, y0 + 0.008), (r, y0 + 0.012)]]
    profile += [[(r, h - 0.012), (r, h)], [(r, h), (r * 0.8, h), (0.0, h)]]
    body.add(lathe(profile, 40), "fixture_body")
    body.add(disc(r * 0.92, 40, up=False).transform(T(0, -h + 0.003, 0)), "lens")
    body.children.append(Node("lens", T(0, -h - 0.04, 0)))
    yoke.children.append(body)
    root.children.append(yoke)
    return [root]


def build_moving_head() -> List[Node]:
    """moving-head at 150 x 220 x 120 mm: clamp, base, a yoke that pans and a
    head that tilts between its arms, lens down at home."""
    hx, hy, hz = 0.075, 0.11, 0.06              # head half extents; Y is the beam
    arm_x, arm_y = hx + 0.05, hy + 0.08
    pivot = -0.39
    root = Node("moving_head", extras={"dimensions_mm": [150, 220, 120], "lens_radius": round(min(hx, hz) * 0.85, 4)})
    root.add(merge(clamp_parts() + [rod((0, -0.07, 0), (0, -0.03, 0), 0.01, 10)]), "fixture_body")
    base = Node("base", T(0, -0.08, 0))
    base.add(rounded_box((arm_x + 0.04, 0.05, hz + 0.06), 0.014), "fixture_body")
    base.add(rounded_box((0.03, 0.012, 0.002), 0.002, center=(0.07, 0.0, hz + 0.061)), "display")
    root.children.append(base)
    yoke = Node("yoke", T(0, pivot, 0))
    parts = [rounded_box((arm_x, 0.03, hz * 0.95), 0.01, center=(0, arm_y * 1.1, 0))]
    for sx in (-1, 1):
        parts.append(rounded_box((0.028, arm_y * 0.6, hz * 0.85), 0.012, center=(sx * arm_x, arm_y * 0.5, 0)))
    parts.append(cylinder(0.05, 0.0105, 24, 0.003).transform(T(0, 0.2495, 0)))     # the pan bearing
    yoke.add(merge(parts), "fixture_body")
    head = Node("head")
    head.add(rounded_box((hx, hy, hz), 0.025), "fixture_body")
    head.add(merge([cylinder(0.022, 0.012, 20, 0.004).transform(T(sx * (hx + 0.012), 0, 0) @ Rz(math.pi / 2))
                    for sx in (-1, 1)]), "fixture_body")
    lens_r = min(hx, hz) * 0.85
    head.add(lathe([[(lens_r, 0.0), (lens_r + 0.006, 0.0), (lens_r + 0.006, 0.006)]], 32)
             .transform(T(0, -hy - 0.006, 0)), "fixture_body")
    head.add(disc(lens_r, 32, up=False).transform(T(0, -hy - 0.002, 0)), "lens")
    head.children.append(Node("lens", T(0, -hy - 0.04, 0)))
    yoke.children.append(head)
    root.children.append(yoke)
    return [root]


def build_wall_arm() -> List[Node]:
    """A bracket from a wall: plate at the origin, a 1 m tube along +X that
    the scene scales to length."""
    root = Node("wall_arm")
    root.add(merge([rod((0, 0, 0), (1, 0, 0), 0.016, 12),
                    rounded_box((0.006, 0.06, 0.06), 0.004, center=(0.006, 0, 0))]), "fixture_body")
    return [root]


ASSETS = {
    "venue": build_venue,
    "amp_stack": build_amp_stack,
    "combo": build_combo,
    "bass_cab": build_bass_cab,
    "guitar": build_guitar,
    "drum_kit": build_drum_kit,
    "mic_stand": build_mic_stand,
    "chair": build_chair,
    "bar": build_bar,
    "par_can": build_par_can,
    "moving_head": build_moving_head,
    "wall_arm": build_wall_arm,
}


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    for name, build in ASSETS.items():
        built = build()
        roots, extras = built if isinstance(built, tuple) else (built, None)
        path = os.path.join(OUT_DIR, f"{name}.glb")
        size = GLB().write(path, roots, extras)
        print(f"{name + '.glb':18s} {size / 1024:8.1f} KiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
