"""Machine-readable discovery for Twin's supported command contract."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING

from twin.resources import SCHEMA_NAMES, ResourceCatalog

if TYPE_CHECKING:
    import argparse


CONTRACT_VERSION = 1
_DEVELOPMENT_PACKAGE_VERSION = "0+development"


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
            for name in SCHEMA_NAMES
        },
        "commands": exported,
        "action_commands": action_commands,
    }


def render_agent_integration(
    parser: "argparse.ArgumentParser", resources: ResourceCatalog
) -> str:
    """Render the checked-in agent guide from the live command contract."""
    contract = render_contract(parser, resources)
    commands = contract["commands"]
    assert isinstance(commands, dict)
    lines = [
        "<!-- Generated from `twin contract --json`; do not edit by hand. -->",
        "",
        "# Twin agent integration",
        "",
        "Agents discover Twin only through `twin contract --json`, then invoke the exact",
        "console command named there. Submission tokens and schema paths are emitted at runtime",
        "and must be consumed literally.",
        "",
        "## Commands",
        "",
    ]
    for name, raw_command in commands.items():
        assert isinstance(name, str)
        assert isinstance(raw_command, dict)
        argv = raw_command["argv"]
        output = raw_command["output"]
        assert isinstance(argv, list)
        assert isinstance(output, dict)
        lines.append(f"- `{name}`: `{' '.join(str(part) for part in argv)}`")
        lines.append(f"  - output: `{output['shape']}`")
        continuation_field = output.get("continuation_field")
        if isinstance(continuation_field, str):
            lines.append(f"  - continuation: `{continuation_field}`")
        schema_path = output.get("schema_path")
        if isinstance(schema_path, str):
            lines.append(f"  - schema: `{_relative_resource_path(schema_path, resources)}`")
    action_commands = contract["action_commands"]
    assert isinstance(action_commands, list)
    lines.extend([
        "",
        "## Lifecycle continuation",
        "",
        "Complete each action through its returned `submit.argv`. After submission, continue from the returned workspace result:",
        "execute `next_command.argv` exactly when it is non-null, and repeat with each returned result until it is null.",
        "Do not derive continuation commands from status names.",
        "",
        "## Action-only submissions",
        "",
        "The following commands are intentionally omitted from interactive help and are returned",
        "only as action handoffs: " + ", ".join(f"`{name}`" for name in action_commands) + ".",
        "",
    ])
    return "\n".join(lines)


def _relative_resource_path(path: str, resources: ResourceCatalog) -> str:
    try:
        return str(Path(path).relative_to(resources.root))
    except ValueError:
        return path


def _package_version() -> str:
    try:
        return version("xuejiao-twin")
    except PackageNotFoundError:
        return _DEVELOPMENT_PACKAGE_VERSION
