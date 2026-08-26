"""Deterministic manifests for Twin's packaged and installed skill trees."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path


MANIFEST_FILENAME = "manifest.json"
MANIFEST_VERSION = 1


def build_skill_manifest(skill_dir: Path) -> dict[str, object]:
    """Describe the skill tree without following links or self-hashing the manifest."""
    if not skill_dir.is_dir() or skill_dir.is_symlink():
        raise ValueError(f"skill directory is missing or not a real directory: {skill_dir}")
    entries: list[dict[str, object]] = []
    _append_directory_entries(skill_dir, Path(), entries)
    return {"manifest_version": MANIFEST_VERSION, "entries": entries}


def render_skill_manifest(manifest: dict[str, object]) -> str:
    """Render a manifest in its canonical checked-in form."""
    return json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def expected_skill_manifest(skill_dir: Path) -> tuple[dict[str, object], str]:
    """Return the live tree manifest after verifying the checked-in manifest is current."""
    manifest = build_skill_manifest(skill_dir)
    rendered = render_skill_manifest(manifest)
    manifest_path = skill_dir / MANIFEST_FILENAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError(f"packaged Twin skill manifest drift: missing real file {manifest_path}")
    if manifest_path.read_text(encoding="utf-8") != rendered:
        raise ValueError(
            "packaged Twin skill manifest drift: run scripts/generate-skill-manifest.py"
        )
    return manifest, rendered


def installed_skill_drift(packaged: Path, installed: Path) -> str | None:
    """Return a fail-closed drift description, or None when both trees match."""
    try:
        expected, expected_manifest_text = expected_skill_manifest(packaged)
    except (OSError, ValueError) as exc:
        return str(exc)
    if not installed.is_dir() or installed.is_symlink():
        return f"installed Twin skill drift: missing real directory {installed}"

    packaged_manifest = packaged / MANIFEST_FILENAME
    installed_manifest = installed / MANIFEST_FILENAME
    try:
        packaged_manifest_entry = _file_entry(packaged_manifest, MANIFEST_FILENAME)
        installed_manifest_entry = _file_entry(installed_manifest, MANIFEST_FILENAME)
        if installed_manifest.read_text(encoding="utf-8") != expected_manifest_text:
            raise ValueError("manifest content differs")
    except (OSError, ValueError) as exc:
        return f"installed Twin skill drift: changed {MANIFEST_FILENAME}: {exc}"
    if packaged_manifest_entry != installed_manifest_entry:
        return f"installed Twin skill drift: changed {MANIFEST_FILENAME}"

    try:
        actual = build_skill_manifest(installed)
    except (OSError, ValueError) as exc:
        return f"installed Twin skill drift: {exc}"
    differences = _manifest_differences(expected, actual)
    if differences:
        return "installed Twin skill drift: " + "; ".join(differences)
    return None


def _append_directory_entries(
    root: Path, relative_dir: Path, entries: list[dict[str, object]]
) -> None:
    directory = root / relative_dir
    with os.scandir(directory) as scanned:
        children = sorted(scanned, key=lambda entry: entry.name)
    for child in children:
        relative = relative_dir / child.name
        if relative == Path(MANIFEST_FILENAME):
            continue
        metadata = child.stat(follow_symlinks=False)
        entry = _entry_from_stat(Path(child.path), relative.as_posix(), metadata)
        entries.append(entry)
        if entry["type"] == "directory":
            _append_directory_entries(root, relative, entries)


def _entry_from_stat(path: Path, relative: str, metadata: os.stat_result) -> dict[str, object]:
    mode = f"{stat.S_IMODE(metadata.st_mode):04o}"
    if stat.S_ISREG(metadata.st_mode):
        return _file_entry(path, relative, metadata=metadata)
    if stat.S_ISDIR(metadata.st_mode):
        return {"path": relative, "type": "directory", "mode": mode}
    if stat.S_ISLNK(metadata.st_mode):
        target = os.readlink(path)
        encoded = os.fsencode(target)
        return {
            "path": relative,
            "type": "symlink",
            "mode": mode,
            "size": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
    raise ValueError(f"unsupported skill entry type: {relative}")


def _file_entry(
    path: Path, relative: str, *, metadata: os.stat_result | None = None
) -> dict[str, object]:
    metadata = metadata or path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("not a regular file")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "path": relative,
        "type": "file",
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        "size": metadata.st_size,
        "sha256": digest.hexdigest(),
    }


def _manifest_differences(
    expected: dict[str, object], actual: dict[str, object]
) -> list[str]:
    expected_entries = {
        str(entry["path"]): entry for entry in expected.get("entries", [])  # type: ignore[union-attr]
    }
    actual_entries = {
        str(entry["path"]): entry for entry in actual.get("entries", [])  # type: ignore[union-attr]
    }
    missing = sorted(expected_entries.keys() - actual_entries.keys())
    extra = sorted(actual_entries.keys() - expected_entries.keys())
    changed = sorted(
        path
        for path in expected_entries.keys() & actual_entries.keys()
        if expected_entries[path] != actual_entries[path]
    )
    differences = []
    if missing:
        differences.append("missing " + ", ".join(missing))
    if extra:
        differences.append("extra " + ", ".join(extra))
    if changed:
        differences.append("changed " + ", ".join(changed))
    return differences
