# Phase 2b — Fixtures get a transform

*Implementation plan for section 2.b of [`3d-stage-draft.md`](3d-stage-draft.md):
put a real position in metres and a real orientation on every patched fixture,
migrate the existing picture coordinates onto them, and persist both — while the
2D stage keeps rendering exactly what it renders today.*

Written 2026-08-14 against `master`.

---

## 1. What this is, and what it is not

**It is a data change.** `FixtureInstance` gains a metric transform, `show.json`
gains the fields, the two places that write a fixture's position start writing
both halves, and the round trip is tested. Nothing renders differently, no new
widget appears, and no GL is involved.

**It is not:** the room (that is 2a), the 3D view, the light list, the camera, or
any way for a user to *edit* yaw and pitch — the inspector's existing MOUNTING
toggle stays the only orientation control until the designer UI in phase 5. The
new angles will be written by the migration and read by nothing yet. That is the
point: every later phase stops being blocked on a data migration.

**Why it is worth doing alone.** The migration is the only irreversible step in
the whole 3D effort — once shows are on disk with metric positions, changing the
convention means touching user data. Landing it while nothing consumes it means
the convention can still be corrected for free.

---

## 2. Decisions taken

Three questions had to be answered before any of this could be written down.

### 2.1 Scale: the picture migrates onto a real venue

`stage/metrics.py:20` says `MM_PER_STAGE_PX = 2.5`, which makes the 1536×1024
backdrop 3.84 m × 2.56 m — coherent, and a doll's house. Everything from phase 2
onward is tuned against whatever this number is, so it gets fixed now at
**6.0 mm per picture pixel**:

```
1536 px × 6.0 mm  =  9.216 m wide
1024 px × 6.0 mm  =  6.144 m tall
depth              →  not in the picture; every fixture migrates to z = 0
```

Close to `scripts/stage3d_draft.py`'s 9 × 5 × 11 m room, and a rig a human
recognises.

> **The trap.** This is **not** `stage/metrics.py`'s `MM_PER_STAGE_PX`, and that
> constant must not change. `MM_PER_STAGE_PX` answers *how big to draw a body
> inside the picture* — it is clamped between `MIN_BODY_PX` and `MAX_BODY_PX`,
> and `preview_def` throws it away entirely. The new constant answers *where in
> the room a picture coordinate lands*. They were never the same question; they
> merely shared a number. The 2D stage is a picture that reads, not a scale
> drawing — which is exactly why the 3D renderer will take body size straight
> from `physical.dimensions_mm` and never from either constant.

Rejected: keeping 2.5 (a unit relabel that leaves every later phase tuned against
a doll's house), and a per-show scale (pulls the room out of 2a into 2b for a
case — a swapped backdrop of a different pixel size — that the 3D designer
resolves properly later).

### 2.2 Orientation: `hanging` + yaw + pitch

`hanging` already exists, is already persisted, and is already load-bearing:
`stage/geometry.py`'s `Aim` reads it, the profiles flip `body.housing` on it, and
`FixtureInspector`'s MOUNTING switch edits it. It stays exactly as it is — the
**mounting**, meaning both the housing flip and which way the fixture's home aim
points. Two floats are added beside it:

| field | meaning |
|---|---|
| `yaw_deg` | rotation about world **+Y**. `0` faces the audience; **positive turns the fixture to the audience's right** (clockwise seen from above). |
| `pitch_deg` | rake off home, about the fixture's own local right axis. `0` is home; **positive always tips the aim toward the front**, whichever way the fixture is mounted. |

The aim vector a later phase builds from them, written out here so it does not
get re-decided:

```python
home  = (0.0, -1.0, 0.0) if hanging else (0.0, 1.0, 0.0)
pitch = (0.0, -cos(p), sin(p)) if hanging else (0.0, cos(p), sin(p))
aim   = rotate_y(pitch, yaw)
```

Check it: hanging with `p=0` is straight down; hanging with `p=90` points at the
audience; standing with `p=90` points at the audience too. A floor par raking the
back wall is `hanging=False, yaw=180, pitch=38`.

`hanging` and `pitch` overlap — `(hanging=True, pitch=180)` and
`(hanging=False, pitch=0)` aim identically and differ only in which way up the
chassis is drawn. That is harmless and deliberate: the UI offers only `hanging`,
and the two mean different physical things (a fixture bolted upside down to a
truss vs. one standing on its feet).

Roll is not modelled. A fixture rolled on its clamp is rare, adds a third angle
nobody can edit, and nothing in phases 3–4 reads it.

Rejected: yaw alone (a raked floor par is not expressible, and that is half of
what a floor package *is*), and a persisted aim vector (three numbers with one
constraint, awkward in a UI, and it overlaps `hanging` instead of composing with
it).

### 2.3 Truth: metres are canonical, picture pixels are derived

One source of truth or the two renderers drift, and reconciling them later is
worse than paying for it now. So:

- **`show.json` stores the metric transform.** `x`/`y` keep being written too, so
  a show stays readable by a build that predates this.
- **`x`/`y` are recomputed from metres** at every point that establishes a
  position — on load, and in both stage writers.
- **The 2D stage is untouched.** It reads `x`/`y` as it always has, and its drag
  handler still writes ints; it just back-projects to metres in the same handler.
  The conversion is affine and pixels are integers, so `px → m → px` is exact —
  which is what makes "no visual change" a checkable claim rather than a hope.

---

## 3. The coordinate convention

Right-handed, metres, matching `scripts/stage3d_draft.py`:

```
+X  audience right          origin: centre of the picture's width,
+Y  up, 0 = the deck                at deck level,
+Z  toward the audience             on the rig plane (z = 0)
−Z  upstage / back wall
```

Origin at the centre of the floor rather than a room corner, so that when 2a
lands a `Room` and the user resizes it, the rig does not slide.

The mapping, with `W, H` the scene rect (default `1536 × 1024`, from
`stage/controller.py:20-21`) and `k = 0.006` m/px:

```
x_m = (x_px − W/2) · k          x_px = round(x_m / k + W/2)
y_m = (H − y_px)   · k          y_px = round(H − y_m / k)
z_m = 0.0                       (the picture has no depth)
```

The image's **bottom** is the floor, which is why `y` flips. Worked through on
the shipped patch: a blinder at `(575, 500)` becomes `(−1.158, 3.144, 0.0)` —
1.16 m stage-left of centre, 3.14 m up. `DECK_TOP = 0.66`
(`stage/controller.py:682`), the height above which `add_fixture` guesses a
fixture is flown, lands at 2.088 m. Both read as a real rig.

---

## 4. The work

Six steps. Each is independently verifiable; the suite stays green after every
one.

### Step 1 — `core/world.py`, the conversion, Qt-free

**The draft says `stage/world.py`. It cannot go there.** `core/config.py` needs
this conversion to parse a patch, and `core/` must never import `stage/` — that
inverts the layering CLAUDE.md sets out. It goes in `core/`, which is also where
2a's `Room` will want to live, next to the patch it describes.

```python
MM_PER_PICTURE_PX = 6.0                 # NOT stage/metrics.py's 2.5 — see §2.1
PICTURE_SIZE_PX = (1536, 1024)

def picture_to_world(x_px, y_px, size=PICTURE_SIZE_PX) -> tuple[float, float, float]
def world_to_picture(x_m, y_m, size=PICTURE_SIZE_PX) -> tuple[int, int]

def sync_from_picture(target, x_px, y_px, size=PICTURE_SIZE_PX) -> None
    """Write the metric x/y a picture position implies, KEEPING depth."""

def sync_from_world(target, size=PICTURE_SIZE_PX) -> None
    """Write the picture x/y the metric position implies."""
```

The two `sync_*` helpers set attributes on a duck-typed target, so this module
imports nothing from `core.config` and there is no cycle. `sync_from_picture`
leaving `z_m` alone is load-bearing: a fixture dragged on the 2D stage must not
lose the depth someone gave it in 3D.

Also here: the clamp bounds (`±100 m` horizontally, `0…30 m` up), yaw wrapped to
`[0, 360)`, pitch clamped to `±180`.

*Verify:* `uv run pytest tests/test_world.py` — see §5.

### Step 2 — the fields on the patch

`core/config.py:185` and `core/config.py:206`:

```python
@dataclass
class FixturePosition:      # one body of a multi-body fixture
    x: int
    y: int
    x_m: float = 0.0
    y_m: float = 0.0
    z_m: float = 0.0

@dataclass
class FixtureInstance:
    ...
    x: int = 0              # picture pixels — derived, see §2.3
    y: int = 0
    x_m: float = 0.0        # metres, canonical
    y_m: float = 0.0
    z_m: float = 0.0
    yaw_deg: float = 0.0
    pitch_deg: float = 0.0
    hanging: bool = True
    bars: List[FixturePosition] = field(default_factory=list)
```

Flat floats, matching the existing flat style, and every one defaulted — so
`FixturePosition(150, 350)` in the tests keeps working and no construction site
in the tree needs touching.

Bars carry position only. Orientation belongs to the fixture: `_make_render_objs`
(`stage/controller.py:486`) already hands every body the same `hanging`, and a
multi-body fixture is one fixture.

**No `__post_init__` deriving one from the other.** It cannot tell "unset" from
"deliberately at the origin" — and the case it would get wrong is precisely the
one phase 5 needs, a fixture constructed from metres with picture pixels left at
their defaults. The pairing is established at the three explicit points below
instead. The cost is that a `FixtureInstance` hand-built in a test carries
`(0, 0, 0)` metres; harmless, because nothing in 2b reads them, and the app's
real construction path (`add_fixture`) syncs.

*Verify:* `uv run pytest` — unchanged, green.

### Step 3 — migration in `_parse_fixtures`

`core/config.py:645-709`, the one place a patch becomes objects — used by
`config.json`, by `show.json` (`core/show/manifest.py:114`) and by the show
migration alike. One branch:

```python
if any metric key present in entry:
    metres = validated from the entry
    x, y   = world_to_picture(x_m, y_m)      # picture follows metres
else:
    x, y   = validated legacy pixels
    metres = picture_to_world(x, y)          # metres follow the picture
```

Either way the pair agrees by construction the moment the object exists. Same
branch for each `bars[]` entry.

> **Do not reuse `Config._validate_float` for the coordinates.**
> `core/config.py:721` returns `min_val` on a malformed value — with a lower
> bound of −100 m, a typo'd `x_m` teleports the fixture 100 m stage-right. Add a
> `_coord(value, lo, hi)` beside it that clamps in range but falls back to
> **0.0**. `_validate_float` is fine for the angles, whose lower bound is
> meaningful.

*Verify:* the round-trip and migration tests in §5.

### Step 4 — persistence

**No code change expected.** `show_to_manifest` serialises fixtures with
`asdict(f)` (`core/show/manifest.py:61`), which picks up new fields and nested
`FixturePosition` dicts for free, and step 3 already reads them back. `.showpack`
bundling copies the manifest wholesale, so it carries them too.

Two things to confirm rather than assume, both covered by tests:

- **`FORMAT_VERSION` stays at 1.** Adding optional keys is backward compatible,
  and a bump would make every new show refuse to open on an older build
  (`core/show/manifest.py:76-79`) to protect against a downgrade path nobody is
  on. The accepted risk: an older build that opens and re-saves a show drops
  `z_m`, `yaw_deg` and `pitch_deg`. `x`/`y` survive, so the fixture stays where
  it was; only depth and rake are lost. Worth stating in the commit message.
- **Migration alone must not dirty a show.** `ShowManager` takes its autosave
  baseline from the *in-memory* show at open (`app/show_manager.py:73`) and
  compares against the in-memory serialisation
  (`app/show_manager.py:203-211`) — never against the bytes on disk. So opening
  an existing show migrates it in memory, writes nothing, and takes no automatic
  version snapshot. The new keys reach disk at the next genuine edit. Verify it,
  because if it were the other way round every show in the user's library would
  silently rewrite and snapshot on first open.

### Step 5 — the two writers

Both live in `stage/controller.py`, and they are the only places in the tree that
set a fixture's position:

- **`_on_fixture_moved` (`:661-675`)** — after writing the int pixels, call
  `world.sync_from_picture(target, pos.x(), pos.y())` on whichever target the
  existing branch picked (the instance, or the `bars[]` entry). Two lines, and
  depth survives the drag.
- **`add_fixture` (`:608-644`)** — sync the instance after construction, and each
  seeded bar inside the existing loop. `hanging=self.is_flown_height(y)` is
  unchanged; `yaw_deg`/`pitch_deg`/`z_m` start at 0.

`fixture_moved` / `fixture_added` already funnel to `save_config`, so persistence
needs nothing new.

*Verify:* the designer tests in §5, plus the app smoke run in §6.

### Step 6 — write it down

- A paragraph in `CLAUDE.md` under *Configuration & persistence*: what a patch
  entry now carries, that metres are canonical and pixels derived, and the
  `MM_PER_PICTURE_PX` / `MM_PER_STAGE_PX` distinction — that is exactly the kind
  of "looks arbitrary, isn't" the file exists for.
- A line in `docs/3d-stage-draft.md` §4 marking 2b landed, pointing here.

---

## 5. Tests

New file, `tests/test_world.py` — Qt-free and pure, like `test_stage_optics.py`:

1. **Round trip is exact.** Over a grid across the full 1536×1024 rect,
   `world_to_picture(picture_to_world(px)) == px`. This is the "no visual change"
   guarantee in one assertion.
2. **Anchors.** Picture centre → `x_m == 0`; the image's bottom edge → `y_m == 0`;
   the top → `y_m == 6.144`; the picture is 9.216 m wide.
3. **`y` flips** — a fixture higher in the picture has a larger `y_m`.
4. **`sync_from_picture` preserves `z_m`** on a target that already has one.
5. **Clamping** — a nonsense coordinate lands at 0.0, not at the lower bound.
6. **`DECK_TOP` in metres** is a sane truss height (≈2.09 m), so the rigging guess
   still means what it says after migration.

Additions to existing files:

- `test_config_roundtrip.py` — a legacy entry (no metric keys) migrates to the
  expected metres; a metric entry round-trips through save/load unchanged; a
  metric entry's `x`/`y` come back recomputed and equal to what the legacy pixels
  would have been; `bars[]` migrate too.
- `test_show_manifest.py` — the new fields survive `show_to_manifest` /
  `show_from_manifest`; a manifest written *without* them still loads.
- `test_show_autosave.py` — opening a show whose manifest has no metric keys
  leaves the autosave state clean and writes nothing (step 4's second point).
- `test_stage_designer.py` — after a simulated drag the metric fields track the
  pixels and `z_m` is untouched; `add_fixture` seeds metres on the instance and
  on every bar it creates.

---

## 6. Success criteria

```
1. uv run pytest                         → green, including the new tests
2. open an existing .show, change nothing, close
                                         → show.json byte-identical, no new
                                            entry in versions/
3. open it, drag one fixture, save        → x/y unchanged in kind, x_m/y_m/z_m
                                            present and consistent; reopening
                                            puts the fixture back in the same
                                            pixel
4. /verify or uv run python main.py       → stage renders identically; a
                                            screenshot before/after the branch
                                            differs in no pixel
```

Criterion 4 is the one that matters. This phase is worthless if it changes the
picture, and it is a bug if it does.

---

## 7. What this unblocks, and what it deliberately leaves broken

Phase 1 (`Stage3DView`) can place a real patch in a real room the day this lands.
Phases 3–4 inherit a transform they do not have to invent.

Left knowingly incomplete:

- **Nothing edits yaw or pitch.** Every migrated fixture is yaw 0, pitch 0 —
  facing the audience, aimed at home. Correct for a rig described by a
  front-elevation picture, since a picture cannot say otherwise, and the 3D
  designer in phase 5 is where a user fixes it.
- **Every fixture is on the plane `z = 0`.** There is no depth in the source
  data. A rig will look flat in 3D until someone moves it, which is the honest
  outcome of migrating a photograph.
- **A swapped backdrop of a non-default pixel size** migrates against the
  default 1536×1024 rect. The conversion takes an explicit `size` argument so the
  caller that knows better can pass it; `_parse_fixtures` does not know better,
  and inventing a way for it to find out is 2a's problem.
