"""Machine-readable discovery for Twin's supported command contract."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

from twin.resources import ResourceCatalog

if TYPE_CHECKING:
    import argparse


CONTRACT_VERSION = 1
_FALLBACK_PACKAGE_VERSION = "0.1.0"


def render_contract(
    parser: "argparse.ArgumentParser", resources: ResourceCatalog
) -> dict[str, object]:
    """Render command metadata attached by :func:`twin.cli.build_parser`."""
    commands = getattr(parser, "_twin_commands", ())
    exported: dict[str, object] = {}
    action_commands: list[str] = []
    for command in commands:
        visibility = getattr(command, "_twin_visibility")
        if visibility not in {"public", "action-only"}:
            continue
        name = getattr(command, "_twin_name")
        output = dict(getattr(command, "_twin_output"))
        schema_path = output.get("schema_path")
        if isinstance(schema_path, str):
            output["schema_path"] = str(resources.root / schema_path)
        exported[name] = {
            "argv": list(getattr(command, "_twin_argv")),
            "output": output,
        }
        if visibility == "action-only":
            action_commands.append(name)
    return {
        "contract_version": CONTRACT_VERSION,
        "package_version": _package_version(),
        "schema_paths": {
            name: str(resources.root / "schemas" / f"twin.{name}.schema.json")
            for name in ("action", "goal", "plan", "run-evidence", "state")
        },
        "commands": exported,
        "action_commands": action_commands,
    }


def _package_version() -> str:
    try:
        return version("xuejiao-twin")
    except PackageNotFoundError:
        return _FALLBACK_PACKAGE_VERSION
