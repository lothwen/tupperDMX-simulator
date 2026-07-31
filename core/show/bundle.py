# ABOUTME: Portable show bundles (.showpack): one zip with the manifest
# ABOUTME: rewritten to relative paths, owned midis, collected audio (collect &
# ABOUTME: save) and the non-shipped fixture defs the patch needs.
import json
import os
import zipfile
from pathlib import Path
from typing import Dict, List

from core import fixture_library
from core.fixture_sidecar import (
    SIDECAR_SUFFIX, parse_sidecar, target_filename,
)
from core.show.model import (
    AUDIO_DIR,
    FIXTURES_DIR,
    FORMAT_VERSION,
    MANIFEST_NAME,
    MIDIS_DIR,
    SHOW_DIR_SUFFIX,
    VERSIONS_DIR,
    Show,
    ShowError,
)
from core.show.manifest import show_to_manifest
from tupperlogger import get_logger

logger = get_logger("show.bundle")

BUNDLE_SUFFIX = ".showpack"


def _def_files_by_name(directory) -> Dict[str, List[Path]]:
    """{registry key: [OFL file, sidecar?]} for one fixtures directory.

    A definition is one or two files — the OFL document and, optionally, the
    `.tdmx.json` sidecar carrying its scene banks — so both have to travel
    together or the bundled fixture arrives without its clips. Keys are the
    registry name (the sidecar's `key` override, else the OFL `name`), not the
    filename.
    """
    out: Dict[str, List[Path]] = {}
    if not directory or not os.path.isdir(str(directory)):
        return out

    paths = sorted(Path(directory).glob("*.json"))

    def read(path):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    sidecars: Dict[str, Path] = {}
    sidecar_data: Dict[str, dict] = {}
    for path in paths:
        if not path.name.endswith(SIDECAR_SUFFIX):
            continue
        data = read(path)
        if data is None:
            continue
        target = target_filename(path.name, parse_sidecar(data, str(path)))
        sidecars[target] = path
        sidecar_data[target] = data

    for path in paths:
        if path.name.endswith(SIDECAR_SUFFIX):
            continue
        data = read(path)
        if data is None:
            continue
        key = str(sidecar_data.get(path.name, {}).get("key")
                  or data.get("name", "")).strip()
        if not key:
            continue
        files = [path]
        if path.name in sidecars:
            files.append(sidecars[path.name])
        out[key] = files
    return out


def used_nonshipped_defs(show: Show) -> Dict[str, List[Path]]:
    """Def files that must travel with the show: every type used by the patch
    whose winning definition comes from the user or show-local layer (unique
    types AND overrides of shipped ones)."""
    candidates: Dict[str, List[Path]] = {}
    candidates.update(_def_files_by_name(fixture_library.fixtures_dir_user()))
    candidates.update(_def_files_by_name(show.fixtures_dir))  # show layer wins
    used_types = {f.type for f in show.fixtures}
    return {t: candidates[t] for t in sorted(used_types) if t in candidates}


def _collect(song_dicts_and_paths, subdir: str, warnings: List[str],
             taken=()) -> Dict[str, str]:
    """Collect & save: map each existing external file to a unique
    subdir/<basename> arcname, rewriting the manifest entries in place.
    Missing files keep their original path (relink on the target machine)."""
    arc_by_src: Dict[str, str] = {}
    used_names = set(taken)
    for song_dict, key, src in song_dicts_and_paths:
        if not src or not os.path.isabs(song_dict.get(key) or ""):
            continue  # empty, or already relative (lives inside the show)
        if not os.path.exists(src):
            warnings.append(f"Missing {key} not bundled: {src}")
            continue
        if src not in arc_by_src:
            base = os.path.basename(src)
            stem, ext = os.path.splitext(base)
            n = 1
            while base in used_names:
                n += 1
                base = f"{stem}-{n}{ext}"
            used_names.add(base)
            arc_by_src[src] = f"{subdir}/{base}"
        song_dict[key] = arc_by_src[src]
    return arc_by_src


def export_bundle(show: Show, dest_zip, *, include_history: bool = False) -> List[str]:
    """Write the whole show as one portable zip. Returns human-readable
    warnings (files that could not be bundled).

    ALL song audio travels: referenced backings and guides are collected
    into audio/, and files already living in the show's audio/ (post-import
    bundles) ship wholesale like midis/ — their filenames are reserved so a
    collected external can't collide."""
    warnings: List[str] = []
    data = show_to_manifest(show)
    songs = list(zip(data["songs"], show.playlist.songs))

    owned_audio_names = ({p.name for p in show.audio_dir.rglob("*") if p.is_file()}
                         if show.audio_dir.is_dir() else set())
    audio_map = _collect(
        [(d, "audio", s.audio) for d, s in songs], AUDIO_DIR, warnings,
        taken=owned_audio_names)
    guide_map = _collect(
        [(d, "guide", s.guide) for d, s in songs], AUDIO_DIR, warnings,
        taken=owned_audio_names | {os.path.basename(a) for a in audio_map.values()})
    # Post-migration midis live inside the show; stray absolute ones (hand-
    # edited manifests) are collected the same way so the bundle stays whole
    # (owned filenames are reserved so a stray twin can't collide).
    owned_midi_names = ({p.name for p in show.midis_dir.rglob("*") if p.is_file()}
                        if show.midis_dir.is_dir() else set())
    midi_map = _collect(
        [(d, "midi", s.midi) for d, s in songs], MIDIS_DIR, warnings,
        taken=owned_midi_names)

    dest_zip = Path(dest_zip)
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST_NAME, json.dumps(data, indent=2))
        for owned_dir, subdir in ((show.midis_dir, MIDIS_DIR),
                                  (show.audio_dir, AUDIO_DIR)):
            if owned_dir.is_dir():
                for path in sorted(owned_dir.rglob("*")):
                    if path.is_file():
                        zf.write(path, f"{subdir}/"
                                       f"{path.relative_to(owned_dir).as_posix()}")
        for src, arc in {**audio_map, **guide_map, **midi_map}.items():
            zf.write(src, arc)
        for _name, def_files in used_nonshipped_defs(show).items():
            for path in def_files:
                zf.write(path, f"{FIXTURES_DIR}/{path.name}")
        if include_history and show.versions_dir.is_dir():
            for path in sorted(show.versions_dir.rglob("*")):
                if path.is_file():
                    zf.write(path, f"{VERSIONS_DIR}/"
                                   f"{path.relative_to(show.versions_dir).as_posix()}")
    logger.info(f"Exported bundle: {dest_zip} ({len(warnings)} warnings)")
    return warnings


def import_bundle(zip_path, dest_parent) -> Path:
    """Extract a .showpack into dest_parent/<Name>.show/ and return the new
    root (open it with store.load_show / ShowManager.open_show)."""
    zip_path = Path(zip_path)
    try:
        zf = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as e:
        raise ShowError(f"Cannot read bundle '{zip_path}': {e}") from e
    with zf:
        try:
            data = json.loads(zf.read(MANIFEST_NAME))
        except KeyError:
            raise ShowError(f"'{zip_path}' is not a show bundle (no {MANIFEST_NAME})")
        except (json.JSONDecodeError, ValueError) as e:
            raise ShowError(f"Bundle manifest is corrupt: {e}") from e
        version = data.get("format_version") if isinstance(data, dict) else None
        if not isinstance(version, int) or version < 1:
            raise ShowError(f"Bundle '{zip_path}' has no valid format_version")
        if version > FORMAT_VERSION:
            raise ShowError(
                f"Bundle uses format_version {version}; "
                f"this app supports up to {FORMAT_VERSION}")

        name = data.get("name") if isinstance(data.get("name"), str) else ""
        name = name or zip_path.stem
        root = Path(dest_parent) / (name + SHOW_DIR_SUFFIX)
        if root.exists():
            raise ShowError(f"'{root}' already exists")

        for info in zf.infolist():
            member = Path(info.filename)
            if member.is_absolute() or ".." in member.parts:
                raise ShowError(f"Unsafe path in bundle: {info.filename}")
        zf.extractall(root)

    for sub in (MIDIS_DIR, AUDIO_DIR, FIXTURES_DIR, VERSIONS_DIR):
        (root / sub).mkdir(exist_ok=True)
    logger.info(f"Imported bundle '{zip_path}' -> {root}")
    return root
