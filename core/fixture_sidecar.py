# ABOUTME: Parser for the .tdmx.json sidecar carrying TupperDMX authoring data
# ABOUTME: that the Open Fixture Library format has nowhere to put.
"""The `.tdmx.json` sidecar.

An OFL document describes a *product*: its channels, what each DMX value does,
its physical shape. It has no room for how this app authors a show with it —
scene banks, chases, song-part tags, which channels follow the blend/knob knobs,
which built-in effect maps to which animation asset. The OFL root is
`additionalProperties: false`, so those can't ride inside it either.

They live in a sibling file instead: `fixtures/ledbar.json` stays a pristine OFL
document that any OFL tool can read, `fixtures/ledbar.tdmx.json` carries the
rest, and `fixtures/ledbar.effects/` holds the artwork it names. A fixture
dropped in straight from the OFL project works with no sidecar at all — it
simply has no scene banks.

    {
      "tdmx": 1,                    // format version (required)
      "fixture": "ledbar.json",     // sibling OFL file; default "<stem>.json"
      "key": "ledbar",              // registry key; default the OFL "name"
      "default_mode": "7ch",
      "modes": {
        "*":   { ...extras applying to every mode... },
        "7ch": { ...overrides for this mode... }
      }
    }

Each mode block holds `channels` (per-channel `label`/`flags` overrides, keyed by
resolved OFL channel key), `banks`, `chases`, `effects` and `stage`.

`effects` maps an `Effect` capability's own `effectName` to the artwork that
illustrates it — `{"Radar": {"asset": "17-radar.gif"}}`. The filename is
resolved against this definition's `<stem>.effects/` folder (see
`fixture_library.effects_dir_for`), never against a global bucket: artwork
belongs to the fixture that declares the effect, so a user-layer or show-local
fixture can ship its own, and two fixtures may each have an `01-ON.gif`. `gif`
is the pre-folder spelling of `asset` and is still read.

`stage` is render hints, read by `stage/`. Five keys, all optional:

    "stage": {
        "size_px":      [width, height],   # on-stage body size, overriding mm
        "bodies":       8,                 # separate housings on one DMX slice
        "body_spacing": 175,               # gap between them, in scene units
        "cells":        2,                 # separately-lit zones on one face
        "beam":  "none" | "wash" | "hard"  # what this fixture puts in the air
    }

`beam` overrides `stage/optics.beam_kind()`, which otherwise works this out
from the fixture's own optics. It exists for the fixture whose gate is not a DMX
channel and so cannot be seen in the data at all — an ETC Source Four's lens tube
is the standing example: it declares a 5–90° range like any zoom wash, and only
`"beam": "hard"` can say it is a profile.

Note the mode blocks merge `channels` per key but replace `stage` **wholesale**
(see `ModeExtras.for_mode`), so a named mode declaring any `stage` key must
repeat every key it still wants from the shared `"*"` block.

What a channel *does* is never declared here — that is what its OFL capabilities
say, and the app reads them directly. `flags` carries the two knob behaviours no
capability implies, and `label` renames a channel whose capabilities describe it
too vaguely to show (a pair of `Generic` ranges commented "Forward"/"Reverse" is
a direction channel, and only a human can say so).

**A sidecar can never change the DMX footprint.** Channel count, order and
addressing come only from the OFL document. That is what keeps saved .mid files
— which bind note N to the N-th channel — correct across an edit here.

Writing
-------
The scene editor edits `banks` (and, when a bank is renamed, the `chases` that
name it); the chase editor edits `chases` and nothing else. Both round-trip the
*raw* JSON rather than re-serialising a parsed `Sidecar`: anything this parser
doesn't model — a key from a newer version, a comment field someone added by
hand — survives an edit untouched, and so does the half the other editor owns.
"""

import copy
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional

from tupperlogger import get_logger

logger = get_logger("fixture_sidecar")

SIDECAR_SUFFIX = ".tdmx.json"
SIDECAR_VERSION = 1

# The block applying to every mode, before any named-mode overrides.
ANY_MODE = "*"


@dataclass(frozen=True)
class ModeExtras:
    """The authoring data for one fixture type in one mode."""
    channels: Mapping[str, dict] = field(default_factory=lambda: MappingProxyType({}))
    banks: Mapping[str, List[dict]] = field(default_factory=lambda: MappingProxyType({}))
    chases: tuple = ()
    effects: Mapping[str, dict] = field(default_factory=lambda: MappingProxyType({}))
    stage: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


EMPTY_EXTRAS = ModeExtras()


@dataclass(frozen=True)
class Sidecar:
    version: int = SIDECAR_VERSION
    fixture: str = ""          # target OFL filename ("" -> "<stem>.json")
    key: str = ""              # registry key override ("" -> the OFL name)
    default_mode: str = ""
    modes: Mapping[str, ModeExtras] = field(default_factory=lambda: MappingProxyType({}))

    def for_mode(self, mode_name: str) -> ModeExtras:
        """Extras for `mode_name`: the "*" block with the named block layered on.

        `channels` merges per channel key so a named mode can tweak one channel;
        everything else replaces wholesale, since a bank set only makes sense as
        a whole.
        """
        shared = self.modes.get(ANY_MODE)
        named = self.modes.get(mode_name)
        if named is None:
            return shared or EMPTY_EXTRAS
        if shared is None:
            return named
        return ModeExtras(
            channels=MappingProxyType({**shared.channels, **named.channels}),
            banks=named.banks or shared.banks,
            chases=named.chases or shared.chases,
            effects=named.effects or shared.effects,
            stage=named.stage or shared.stage,
        )


def _parse_mode_extras(data: Any) -> ModeExtras:
    if not isinstance(data, dict):
        return EMPTY_EXTRAS

    channels = {str(key): value for key, value in (data.get("channels") or {}).items()
                if isinstance(value, dict)}

    # Bank order is semantic: note 36+n selects the n-th bank and the first one
    # is the fixture's boot bank. json.load preserves insertion order; nothing
    # here may re-sort it.
    banks: Dict[str, List[dict]] = {}
    for name, scenes in (data.get("banks") or {}).items():
        if isinstance(scenes, list):
            banks[str(name)] = [s for s in scenes if isinstance(s, dict)]

    raw_chases = data.get("chases")
    chases = tuple(c for c in raw_chases if isinstance(c, dict)) \
        if isinstance(raw_chases, list) else ()

    effects = {str(name): spec for name, spec in (data.get("effects") or {}).items()
               if isinstance(spec, dict)}
    stage = data.get("stage") if isinstance(data.get("stage"), dict) else {}

    return ModeExtras(
        channels=MappingProxyType(channels),
        banks=MappingProxyType(banks),
        chases=chases,
        effects=MappingProxyType(effects),
        stage=MappingProxyType(dict(stage)),
    )


def parse_sidecar(data: Any, source: str = "") -> Optional[Sidecar]:
    """Parse one `.tdmx.json` document, or None if it isn't usable."""
    where = f" in '{source}'" if source else ""
    if not isinstance(data, dict):
        logger.warning(f"Malformed sidecar{where}: expected a JSON object")
        return None

    version = data.get("tdmx")
    if not isinstance(version, int) or isinstance(version, bool):
        logger.warning(f"Malformed sidecar{where}: missing the 'tdmx' version key")
        return None
    if version > SIDECAR_VERSION:
        logger.warning(f"Ignoring sidecar{where}: format version {version} is newer "
                       f"than the supported {SIDECAR_VERSION}")
        return None

    modes = {str(name): _parse_mode_extras(block)
             for name, block in (data.get("modes") or {}).items()}

    return Sidecar(
        version=version,
        fixture=str(data.get("fixture") or ""),
        key=str(data.get("key") or ""),
        default_mode=str(data.get("default_mode") or ""),
        modes=MappingProxyType(modes),
    )


def target_filename(sidecar_path_name: str, sidecar: Optional[Sidecar]) -> str:
    """The OFL filename a sidecar belongs to.

    Explicit `fixture` wins; otherwise `ledbar.tdmx.json` pairs with
    `ledbar.json`.
    """
    if sidecar is not None and sidecar.fixture:
        return sidecar.fixture
    return sidecar_path_name[:-len(SIDECAR_SUFFIX)] + ".json"


def sidecar_filename(ofl_path) -> str:
    """The sidecar filename that pairs with an OFL document by default."""
    return Path(ofl_path).stem + SIDECAR_SUFFIX


# --- writing ------------------------------------------------------------------

def banks_mode_key(document: Any, mode_name: str, single_mode: bool) -> str:
    """Which mode block a fixture's banks should be written into.

    Whichever block already supplies them wins, so an edit lands where the
    author put them. With nothing to go on, a one-mode fixture writes to "*"
    (its scenes can only mean one thing) while a multi-mode fixture writes to
    the named block — scene dicts are keyed by channel field, and two modes
    don't share a channel set.
    """
    modes = document.get("modes") if isinstance(document, dict) else None
    for key in (mode_name, ANY_MODE):
        block = (modes or {}).get(key)
        if isinstance(block, dict) and "banks" in block:
            return key
    return ANY_MODE if single_mode else mode_name


def apply_banks(document: Any, mode_key: str,
                banks: Optional[Mapping[str, List[dict]]] = None,
                chases: Optional[List[dict]] = None) -> dict:
    """A copy of a raw sidecar document with `banks` replaced in `mode_key`.

    Each half is written only when given, so one editor can rewrite the chases
    without speaking for the banks (and vice versa) — the Scenes and Chases tabs
    hold their own copies of the block, and a write that carried both would
    undo whatever the other tab had just done. An empty bank set drops the key
    rather than writing `"banks": {}`; likewise an empty chase list.
    """
    result = copy.deepcopy(document) if isinstance(document, dict) else {}
    result.setdefault("tdmx", SIDECAR_VERSION)
    modes = result.setdefault("modes", {})
    if not isinstance(modes, dict):
        modes = result["modes"] = {}
    block = modes.setdefault(mode_key, {})
    if not isinstance(block, dict):
        block = modes[mode_key] = {}

    if banks is not None:
        if banks:
            block["banks"] = {str(name): list(scenes)
                              for name, scenes in banks.items()}
        else:
            block.pop("banks", None)

    if chases is not None:
        if chases:
            block["chases"] = list(chases)
        else:
            block.pop("chases", None)
    return result


def write_sidecar(path, document: dict) -> None:
    """Write a sidecar via a temp file + os.replace, so a crash mid-write can
    never leave a fixture definition half-written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(document, f, indent=2)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
