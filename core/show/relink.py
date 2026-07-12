# ABOUTME: Missing-media recovery (Qt-free): locate a moved file by basename
# ABOUTME: under a set of known-good roots (other songs' folders, audio/, ...).
import os
from typing import Iterable, List


def find_by_basename(basename: str, roots: Iterable, limit: int = 5) -> List[str]:
    """Search `roots` (recursively, deduped, in order) for files named exactly
    `basename`. Capped at `limit` hits so a huge root can't stall the UI."""
    if not basename:
        return []
    seen = set()
    out: List[str] = []
    for root in roots:
        root = str(root)
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            if basename in files:
                path = os.path.normpath(os.path.join(dirpath, basename))
                if path not in seen:
                    seen.add(path)
                    out.append(path)
                    if len(out) >= limit:
                        return out
    return out
