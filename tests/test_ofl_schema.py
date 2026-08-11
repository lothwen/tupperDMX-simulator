"""
Conformance test: every shipped fixture definition is valid Open Fixture Library.

The premise of the OFL migration is that `fixtures/*.json` are real OFL
documents — readable by OFL's own tooling, and interchangeable with the ~700
manufacturers in the OFL project. Parsing cleanly with `core/ofl.py` doesn't
prove that: our parser is deliberately lenient. This validates against OFL's
actual JSON Schema instead.

The schemas are vendored under `tests/data/ofl_schemas/` (from the upstream
`schema-12.5.1` tag) so the suite stays offline.

Also checks the sidecars, which are the other half of a definition: each one
must pair with a real OFL file and must not try to restate anything the OFL
document already owns.

Run with:
    uv run python tests/test_ofl_schema.py
"""

import sys
import os
import json
import glob

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from core.fixture_sidecar import SIDECAR_SUFFIX, parse_sidecar, target_filename
from core.ofl import parse_ofl

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures")
SCHEMA_DIR = os.path.join(os.path.dirname(__file__), "data", "ofl_schemas")

jsonschema = pytest.importorskip("jsonschema", reason="jsonschema is a dev dependency")


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _load(path):
    with open(path) as f:
        return json.load(f)


def _ofl_files():
    return sorted(p for p in glob.glob(os.path.join(FIXTURES_DIR, "*.json"))
                  if not p.endswith(SIDECAR_SUFFIX))


def _sidecar_files():
    return sorted(glob.glob(os.path.join(FIXTURES_DIR, "*" + SIDECAR_SUFFIX)))


def _validator():
    """A validator for fixture.json with the sibling schemas resolvable.

    OFL's schemas $ref each other by bare filename ("definitions.json#/..."),
    so every schema is registered under that name.
    """
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    resources = []
    for path in sorted(glob.glob(os.path.join(SCHEMA_DIR, "*.json"))):
        schema = _load(path)
        resources.append((os.path.basename(path),
                          Resource.from_contents(schema, default_specification=DRAFT202012)))
    registry = Registry().with_resources(resources)
    fixture_schema = _load(os.path.join(SCHEMA_DIR, "fixture.json"))
    return jsonschema.Draft202012Validator(fixture_schema, registry=registry)


def test_shipped_definitions_validate():
    print("test_shipped_definitions_validate")
    validator = _validator()
    files = _ofl_files()
    _assert(len(files) >= 4, f"{len(files)} shipped fixture definitions found")
    for path in files:
        errors = sorted(validator.iter_errors(_load(path)), key=lambda e: list(e.path))
        detail = "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:3])
        _assert(not errors, f"{os.path.basename(path)} is valid OFL 12.5.1 {detail}")


def test_mode_names_avoid_the_forbidden_substring():
    print("test_mode_names_avoid_the_forbidden_substring")
    # modeNameString is nonEmptyString AND ^((?!mode)(?!Mode).)*$ — easy to trip
    # when hand-writing a def ("8ch mode", "Extended Mode").
    for path in _ofl_files():
        for mode in _load(path)["modes"]:
            name = mode["name"]
            _assert("mode" not in name.lower(),
                    f"{os.path.basename(path)}: mode name {name!r} is legal")


def test_every_sidecar_pairs_with_a_fixture():
    print("test_every_sidecar_pairs_with_a_fixture")
    available = {os.path.basename(p) for p in _ofl_files()}
    sidecars = _sidecar_files()
    _assert(sidecars, "sidecars are shipped alongside the definitions")
    for path in sidecars:
        sidecar = parse_sidecar(_load(path), path)
        _assert(sidecar is not None, f"{os.path.basename(path)} parses")
        target = target_filename(os.path.basename(path), sidecar)
        _assert(target in available,
                f"{os.path.basename(path)} pairs with {target}")


def test_sidecars_only_reference_real_channels():
    print("test_sidecars_only_reference_real_channels")
    # A role/flag override keyed by a channel the fixture doesn't have is dead
    # weight that silently does nothing — usually a typo.
    for path in _sidecar_files():
        sidecar = parse_sidecar(_load(path), path)
        target = target_filename(os.path.basename(path), sidecar)
        fixture = parse_ofl(_load(os.path.join(FIXTURES_DIR, target)))
        known = set(fixture.available_channels)
        for mode_name, extras in sidecar.modes.items():
            for channel_key in extras.channels:
                _assert(channel_key in known,
                        f"{os.path.basename(path)} [{mode_name}]: "
                        f"'{channel_key}' is a real channel")


def test_bank_scenes_only_reference_real_channels():
    print("test_bank_scenes_only_reference_real_channels")
    # Scene values are keyed by channel key; a stale key would silently render
    # as 0 rather than erroring.
    metadata = {"name", "song_parts"}
    for path in _sidecar_files():
        sidecar = parse_sidecar(_load(path), path)
        target = target_filename(os.path.basename(path), sidecar)
        fixture = parse_ofl(_load(os.path.join(FIXTURES_DIR, target)))
        # A fine byte is a channel key too: it takes a DMX slot of its own, and
        # a mode that uses one expects every scene to carry it (load_scenes
        # skips a scene missing any of the mode's fields).
        known = set(fixture.available_channels) | {
            alias for channel in fixture.available_channels.values()
            for alias in channel.fine_aliases}
        for mode_name, extras in sidecar.modes.items():
            for bank, scenes in extras.banks.items():
                stray = {key for scene in scenes for key in scene} - known - metadata
                _assert(not stray,
                        f"{os.path.basename(path)} [{mode_name}] {bank}: "
                        f"every scene key is a channel {sorted(stray)}")


def test_chases_reference_real_banks():
    print("test_chases_reference_real_banks")
    for path in _sidecar_files():
        sidecar = parse_sidecar(_load(path), path)
        for mode_name, extras in sidecar.modes.items():
            for index, chase in enumerate(extras.chases):
                for bank in chase.get("banks", []):
                    _assert(bank in extras.banks,
                            f"{os.path.basename(path)} [{mode_name}] chase {index}: "
                            f"bank '{bank}' exists")


def main():
    test_shipped_definitions_validate()
    test_mode_names_avoid_the_forbidden_substring()
    test_every_sidecar_pairs_with_a_fixture()
    test_sidecars_only_reference_real_channels()
    test_bank_scenes_only_reference_real_channels()
    test_chases_reference_real_banks()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
