"""Owner-safe installation of the Twin host skill."""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from twin.paths import TwinPaths
from twin.resources import ResourceCatalog
from twin.skill_manifest import expected_skill_manifest, installed_skill_drift


_HOST_SKILLS = {
    "cursor_skill": Path(".cursor/skills/twin"),
    "codex_skill": Path(".codex/skills/twin"),
    "antigravity_skill": Path(".gemini/antigravity-cli/skills/twin"),
}
_CLAUDE_SKILLS = Path(".claude/skills")
_CURSOR_SKILLS = Path(".cursor/skills")
_CUTOVER_INSTRUCTION = "complete the additive-registry cutover first"


@dataclass(frozen=True)
class LinkResult:
    name: str
    ok: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "detail": self.detail}


def install_skill(
    paths: TwinPaths, resources: ResourceCatalog, home: Path
) -> list[LinkResult]:
    """Copy Twin's packaged skill and link each supported host directly to it."""
    home = home.expanduser().resolve()
    packaged_skill = resources.skill_dir()
    expected_skill_manifest(packaged_skill)
    cursor_skills = home / _CURSOR_SKILLS
    _validate_cursor_registry(cursor_skills)
    _validate_claude_registry(home, cursor_skills)
    targets = {name: home / relative for name, relative in _HOST_SKILLS.items()}
    for target in targets.values():
        _assert_replaceable_twin_link(target, paths.root)

    _ensure_cursor_registry(cursor_skills)
    _ensure_claude_registry(home, cursor_skills)
    installed = paths.installed_skills / "twin"
    _copy_skill_atomically(packaged_skill, installed)
    for target in targets.values():
        target.parent.mkdir(parents=True, exist_ok=True)
        if _exists(target):
            target.unlink()
        target.symlink_to(installed, target_is_directory=True)
    return check_skill_links(paths, home, resources=resources)


def check_skill_links(
    paths: TwinPaths, home: Path, *, resources: ResourceCatalog
) -> list[LinkResult]:
    """Report whether host entries point directly at Twin's installed skill copy."""
    home = home.expanduser().resolve()
    installed = paths.installed_skills / "twin"
    installed_ok = installed.is_dir() and not installed.is_symlink()
    try:
        packaged_skill = resources.skill_dir()
    except FileNotFoundError as exc:
        drift = f"packaged Twin skill manifest drift: {exc}"
    else:
        drift = installed_skill_drift(packaged_skill, installed)
    cursor_skills = home / _CURSOR_SKILLS
    cursor_error = _cursor_registry_error(cursor_skills)
    results = [
        LinkResult(
            "installed_skill",
            drift is None,
            str(installed) if drift is None else drift,
        )
    ]
    for name, relative in _HOST_SKILLS.items():
        if name == "cursor_skill" and cursor_error is not None:
            results.append(LinkResult(name, False, cursor_error))
        else:
            results.append(_link_result(name, home / relative, installed, installed_ok))
    claude_skills = home / _CLAUDE_SKILLS
    if not claude_skills.is_symlink():
        results.append(LinkResult("claude_skill", False, f"missing skill link: {claude_skills}"))
    elif _resolved(claude_skills) != cursor_skills.resolve():
        results.append(LinkResult("claude_skill", False, f"skill link points elsewhere: {claude_skills}"))
    else:
        results.append(_link_result("claude_skill", claude_skills / "twin", installed, installed_ok))
    return results


def uninstall_skill(paths: TwinPaths, home: Path) -> list[LinkResult]:
    """Remove only Twin-owned host entries and the installed Twin skill copy."""
    home = home.expanduser().resolve()
    results = []
    for name, relative in _HOST_SKILLS.items():
        target = home / relative
        if target.is_symlink() and _is_inside(_resolved(target), paths.root):
            target.unlink()
            results.append(LinkResult(name, True, f"removed skill link: {target}"))
        elif _exists(target):
            results.append(LinkResult(name, True, f"preserved user-owned entry: {target}"))
        else:
            results.append(LinkResult(name, True, f"skill link absent: {target}"))

    installed = paths.installed_skills / "twin"
    if installed.is_symlink() or installed.is_file():
        installed.unlink()
    elif installed.is_dir():
        shutil.rmtree(installed)
    results.append(LinkResult("claude_skill", True, "preserved shared Claude registry link"))
    return results


def _ensure_cursor_registry(cursor_skills: Path) -> None:
    _validate_cursor_registry(cursor_skills)
    cursor_skills.mkdir(parents=True, exist_ok=True)


def _validate_cursor_registry(cursor_skills: Path) -> None:
    error = _cursor_registry_error(cursor_skills)
    if error is not None:
        raise ValueError(error)


def _cursor_registry_error(cursor_skills: Path) -> str | None:
    if cursor_skills.is_symlink():
        return f"{cursor_skills} is a legacy whole-registry symlink; {_CUTOVER_INSTRUCTION}"
    if _exists(cursor_skills) and not cursor_skills.is_dir():
        return f"{cursor_skills} must be a real directory"
    return None


def _ensure_claude_registry(home: Path, cursor_skills: Path) -> None:
    claude_skills = home / _CLAUDE_SKILLS
    _validate_claude_registry(home, cursor_skills)
    if not _exists(claude_skills):
        claude_skills.parent.mkdir(parents=True, exist_ok=True)
        claude_skills.symlink_to(cursor_skills, target_is_directory=True)


def _validate_claude_registry(home: Path, cursor_skills: Path) -> None:
    claude_skills = home / _CLAUDE_SKILLS
    if _exists(claude_skills) and (
        not claude_skills.is_symlink() or _resolved(claude_skills) != cursor_skills.resolve()
    ):
        raise ValueError(f"refusing to replace user-owned {claude_skills}")


def _copy_skill_atomically(source: Path, installed: Path) -> None:
    installed.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=installed.parent, prefix=".twin-skill-") as raw:
        temporary = Path(raw) / "twin"
        shutil.copytree(source, temporary)
        backup = Path(raw) / "previous-twin"
        if _exists(installed):
            os.replace(installed, backup)
        try:
            os.replace(temporary, installed)
        except OSError:
            if _exists(backup):
                os.replace(backup, installed)
            raise


def _assert_replaceable_twin_link(target: Path, twin_root: Path) -> None:
    if not _exists(target):
        return
    if target.is_symlink() and _is_inside(_resolved(target), twin_root):
        return
    raise ValueError(f"refusing to replace user-owned {target}")


def _link_result(name: str, target: Path, installed: Path, installed_ok: bool) -> LinkResult:
    if not installed_ok:
        return LinkResult(name, False, f"installed Twin skill is missing or not a real directory: {installed}")
    if not target.is_symlink():
        return LinkResult(name, False, f"missing skill link: {target}")
    if _resolved(target) != installed.resolve():
        return LinkResult(name, False, f"skill link points elsewhere: {target}")
    return LinkResult(name, True, str(target))


def _exists(path: Path) -> bool:
    return os.path.lexists(path)


def _resolved(path: Path) -> Path:
    return path.resolve(strict=False)


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return False
    return True
