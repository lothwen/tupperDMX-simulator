"""
Characterization test: missing-media relink search (core/show/relink.py).

Covers exact-basename matching under multiple roots, root order precedence,
deduplication, the result cap, and tolerance of nonexistent roots.

Run with:
    uv run python tests/test_show_relink.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.show.relink import find_by_basename


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def test_finds_by_exact_basename_across_roots():
    print("test_finds_by_exact_basename_across_roots")
    with tempfile.TemporaryDirectory() as tmp:
        a = Path(tmp) / "a" / "sub"
        b = Path(tmp) / "b"
        a.mkdir(parents=True)
        b.mkdir()
        (a / "guia.mp3").write_bytes(b"x")
        (b / "guia.mp3").write_bytes(b"x")
        (b / "otra.mp3").write_bytes(b"x")

        hits = find_by_basename("guia.mp3", [Path(tmp) / "a", b])
        _assert(hits == [str(a / "guia.mp3"), str(b / "guia.mp3")],
                "matches are found recursively, in root order")
        _assert(find_by_basename("nope.mp3", [Path(tmp)]) == [],
                "no match -> empty list")
        _assert(find_by_basename("guia.mp3", []) == [], "no roots -> empty list")
        _assert(find_by_basename("", [b]) == [], "empty basename -> empty list")


def test_dedup_and_bad_roots():
    print("test_dedup_and_bad_roots")
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "d"
        d.mkdir()
        (d / "guia.mp3").write_bytes(b"x")
        hits = find_by_basename("guia.mp3",
                                [d, d, str(d), Path(tmp) / "missing", None, ""])
        _assert(hits == [str(d / "guia.mp3")],
                "duplicate roots dedupe; missing/None/empty roots are skipped")


def test_result_cap():
    print("test_result_cap")
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(8):
            sub = Path(tmp) / f"r{i}"
            sub.mkdir()
            (sub / "guia.mp3").write_bytes(b"x")
        hits = find_by_basename("guia.mp3", [tmp], limit=3)
        _assert(len(hits) == 3, "results are capped at the limit")


def main():
    test_finds_by_exact_basename_across_roots()
    test_dedup_and_bad_roots()
    test_result_cap()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
