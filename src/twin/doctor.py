"""Installation health checks for the independent Twin package."""
from __future__ import annotations

import os
import shutil
import sys

from twin.paths import TwinPaths
from twin.resources import SCHEMA_NAMES, ResourceCatalog
from twin.runtime.config import load_runtime_config
from twin.setup import check_skill_links
from twin.skill_manifest import expected_skill_manifest


def doctor_report(paths: TwinPaths, resources: ResourceCatalog) -> dict[str, object]:
    """Return installation health without treating provider availability as fatal."""
    skill_links = {
        link.name: link.as_dict()
        for link in check_skill_links(paths, paths.root.parent, resources=resources)
    }
    checks = {
        "python": _python_check(),
        "package_resources": _package_resources_check(resources),
        "state_home": _state_home_check(paths),
        "installed_skill": skill_links["installed_skill"],
        "cursor_skill": skill_links["cursor_skill"],
        "claude_skill": skill_links["claude_skill"],
        "codex_skill": skill_links["codex_skill"],
        "antigravity_skill": skill_links["antigravity_skill"],
        "git": _executable_check("git"),
        "claude": _executable_check("claude"),
        "codex": _executable_check("codex"),
        "gemini": _executable_check("gemini"),
        "runtime_configuration": _runtime_configuration_check(paths),
    }
    required = (
        "python", "package_resources", "state_home", "installed_skill",
        "cursor_skill", "claude_skill",
        "codex_skill", "antigravity_skill", "git",
    )
    return {
        "ok": all(checks[name]["ok"] for name in required),
        "checks": checks,
    }


def _python_check() -> dict[str, object]:
    version = sys.version_info
    return {
        "ok": version >= (3, 9),
        "detail": f"Python {version.major}.{version.minor}.{version.micro}",
    }


def _package_resources_check(resources: ResourceCatalog) -> dict[str, object]:
    try:
        for name in SCHEMA_NAMES:
            resources.schema(name)
        for name in ("supervisor", "worker"):
            resources.persona(name)
        for name in ("goal", "plan"):
            resources.template(name)
        expected_skill_manifest(resources.skill_dir())
    except (FileNotFoundError, ValueError) as exc:
        return {"ok": False, "detail": str(exc)}
    return {"ok": True, "detail": str(resources.root)}


def _state_home_check(paths: TwinPaths) -> dict[str, object]:
    try:
        paths.root.mkdir(parents=True, exist_ok=True)
        probe = paths.root / ".doctor-write-probe"
        descriptor = os.open(str(probe), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        probe.unlink()
    except OSError as exc:
        return {"ok": False, "detail": str(exc)}
    return {"ok": True, "detail": str(paths.root)}


def _executable_check(name: str) -> dict[str, object]:
    location = shutil.which(name)
    if location is None:
        return {"ok": False, "detail": f"{name} is not installed"}
    return {"ok": True, "detail": location}


def _runtime_configuration_check(paths: TwinPaths) -> dict[str, object]:
    try:
        config = load_runtime_config(paths.config)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return {"ok": False, "detail": str(exc)}
    return {
        "ok": True,
        "detail": f"{config.adapter}/{config.worker_provider}: {paths.config}",
    }
