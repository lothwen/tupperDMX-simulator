# ABOUTME: Builds synthetic OFL documents and FixtureDefs for tests, so a
# ABOUTME: capability nobody ships a fixture for can still be exercised.
"""Synthetic OFL fixtures.

The repo ships four definitions between them covering seven of OFL's 43
capability types. Everything else — pan, gobo wheels, prisms, fog — has to be
written by the test that needs it, so this is the one place that knows how to
spell a valid OFL document.

Not a conftest fixture on purpose: every test module here is also runnable
standalone (`uv run python tests/test_fixture_state.py`), so this has to be a
plain importable module.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import ofl
from core.fixture_library import build_fixture_def
from core.fixture_sidecar import parse_sidecar

SCHEMA = ("https://raw.githubusercontent.com/OpenLightingProject/"
          "open-fixture-library/master/schemas/fixture.json")
META = {"authors": ["test"], "createDate": "2026-01-01",
        "lastModifyDate": "2026-01-01"}


def cap(cap_type, dmx_range=None, **props):
    """One capability. `dmx_range` only matters on a multi-range channel."""
    body = {"type": cap_type}
    if dmx_range is not None:
        body["dmxRange"] = list(dmx_range)
    body.update(props)
    return body


def channel(cap_type, **props):
    """A channel whose whole range is one capability — the common case."""
    extra = {key: props.pop(key) for key in ("defaultValue", "fineChannelAliases")
             if key in props}
    spec = {"capability": cap(cap_type, **props)}
    spec.update(extra)
    return spec


def ranged(*caps, **spec_props):
    """A channel split into several capabilities, each with its own dmxRange."""
    spec = {"capabilities": list(caps)}
    spec.update(spec_props)
    return spec


def doc(*, name="probe", categories=("Other",), channels=None,
        mode_channels=None, matrix=None, template_channels=None, wheels=None,
        physical=None, modes=None):
    """A complete OFL document, ready for `ofl.parse_ofl`."""
    channels = channels if channels is not None else {"d": channel("Intensity")}
    document = {
        "$schema": SCHEMA,
        "name": name,
        "categories": list(categories),
        "meta": META,
        "availableChannels": channels,
        "modes": modes or [{"name": "test",
                            "channels": list(mode_channels
                                             if mode_channels is not None
                                             else channels)}],
    }
    if matrix is not None:
        document["matrix"] = matrix
    if template_channels is not None:
        document["templateChannels"] = template_channels
    if wheels is not None:
        document["wheels"] = wheels
    if physical is not None:
        document["physical"] = physical
    return document


def fdef(sidecar=None, **kwargs):
    """A `FixtureDef` resolved from a synthetic document, at its first mode."""
    document = doc(**kwargs)
    return build_fixture_def(document["name"], ofl.parse_ofl(document),
                             parse_sidecar(sidecar) if sidecar else None)


RGB_CHANNELS = {
    "r": channel("ColorIntensity", color="Red"),
    "g": channel("ColorIntensity", color="Green"),
    "b": channel("ColorIntensity", color="Blue"),
}
