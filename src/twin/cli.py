"""The focused human and token-bound machine CLI for Twin."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable, Sequence

from twin.contract import render_contract
from twin.doctor import doctor_report
from twin.domain.service import TwinService
from twin.errors import WorkspaceBusyError
from twin.paths import TwinPaths
from twin.resources import ResourceCatalog
from twin.runtime.config import build_runtime, load_runtime_config
from twin.runtime.worktree import GitWorkspaceIsolation
from twin.setup import LinkResult, check_skill_links, install_skill, uninstall_skill
from twin.storage.workspaces import WorkspaceStore


_VISIBILITIES = frozenset({"public", "administrative", "action-only"})


class _VisibleSubparsersAction(argparse._SubParsersAction):
    """Keep action-only parsers parseable while omitting them from human help."""

    def add_parser(self, name: str, **kwargs: object) -> argparse.ArgumentParser:
        visibility = kwargs.pop("visibility", "public")
        parser = super().add_parser(name, **kwargs)
        if visibility == "action-only":
            self._choices_actions.pop()
        return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="twin", description="Twin evidence-driven supervisor")
    parser._twin_commands = []  # type: ignore[attr-defined]
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        action=_VisibleSubparsersAction,
        metavar="{start,run,status,respond,handoff,doctor,contract,setup,uninstall}",
    )

    def add_command(
        name: str,
        *,
        visibility: str,
        argv: list[str],
        output: dict[str, str],
        help_text: str,
    ) -> argparse.ArgumentParser:
        if visibility not in _VISIBILITIES:
            raise ValueError(f"unsupported command visibility: {visibility}")
        command = subparsers.add_parser(name, help=help_text, visibility=visibility)
        command.set_defaults(_twin_name=name, _twin_visibility=visibility)
        command._twin_name = name  # type: ignore[attr-defined]
        command._twin_visibility = visibility  # type: ignore[attr-defined]
        command._twin_argv = argv  # type: ignore[attr-defined]
        command._twin_output = output  # type: ignore[attr-defined]
        parser._twin_commands.append(command)  # type: ignore[attr-defined]
        return command

    action_schema = "schemas/twin.action.schema.json"
    result_output = {
        "shape": "workspace-result",
        "continuation_field": "next_command",
    }
    start = add_command(
        "start", visibility="public",
        argv=["start", "<goal>", "--supervisor", "host/<provider>", "--json"],
        output={"shape": "action", "schema_path": action_schema},
        help_text="create a workspace and request authoring",
    )
    start.add_argument("goal")
    start.add_argument("--supervisor", required=True)
    _add_json_flag(start, required=True)

    run = add_command(
        "run", visibility="public",
        argv=["run", "[workspace]", "--supervisor", "host/<provider>", "--json"],
        output={"shape": "action", "schema_path": action_schema},
        help_text="run a ready workspace task",
    )
    run.add_argument("workspace", nargs="?")
    run.add_argument("--supervisor", required=True)
    _add_json_flag(run, required=True)

    status = add_command(
        "status", visibility="public",
        argv=["status", "[workspace]", "[--json]"],
        output=result_output,
        help_text="show workspace state",
    )
    status.add_argument("workspace", nargs="?")
    _add_json_flag(status)

    respond = add_command(
        "respond", visibility="public",
        argv=["respond", "<answer>", "[--workspace <id>]", "[--json]"],
        output=result_output,
        help_text="answer a workspace question",
    )
    respond.add_argument("answer")
    respond.add_argument("--workspace")
    _add_json_flag(respond)

    handoff = add_command(
        "handoff", visibility="public",
        argv=["handoff", "<workspace>", "--from", "host/<provider>", "--to", "host/<provider>", "--json"],
        output=result_output,
        help_text="move a workspace to another supervisor route",
    )
    handoff.add_argument("workspace")
    handoff.add_argument("--from", dest="from_route", required=True)
    handoff.add_argument("--to", dest="to_route", required=True)
    _add_json_flag(handoff, required=True)

    doctor = add_command(
        "doctor", visibility="administrative", argv=["doctor", "[--json]"],
        output={"shape": "doctor-report"}, help_text="check the local installation",
    )
    _add_json_flag(doctor)

    contract = add_command(
        "contract", visibility="administrative", argv=["contract", "--json"],
        output={"shape": "contract"}, help_text="emit machine-readable command discovery",
    )
    _add_json_flag(contract, required=True)

    setup = add_command(
        "setup", visibility="administrative", argv=["setup", "[--check]", "[--json]"],
        output={"shape": "setup-report"}, help_text="install or check the host skill",
    )
    setup.add_argument("--check", action="store_true")
    _add_json_flag(setup)

    uninstall = add_command(
        "uninstall", visibility="administrative", argv=["uninstall", "[--json]"],
        output={"shape": "uninstall-report"}, help_text="remove Twin-owned host skill links",
    )
    _add_json_flag(uninstall)

    _add_submission_command(
        add_command, "submit-plan",
        ["submit-plan", "--workspace", "<id>", "--supervisor", "host/<provider>",
         "--state-revision", "<int>", "--action-token", "<token>", "--payload-file", "-", "--json"],
        "submit a plan for a pending author action",
        output=result_output,
    )
    _add_submission_command(
        add_command, "submit-review",
        ["submit-review", "--workspace", "<id>", "--supervisor", "host/<provider>",
         "--state-revision", "<int>", "--action-token", "<token>", "--run-id", "<id>",
         "--payload-file", "-", "--json"],
        "submit a review decision",
        output=result_output,
        needs_run_id=True,
    )
    return parser


def parser_help(parser: argparse.ArgumentParser) -> str:
    return parser.format_help()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        paths = _paths_for_home()
        resources = _resource_catalog()
        result = _dispatch(args, paths, resources)
    except (OSError, ValueError, WorkspaceBusyError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _emit(result, as_json=args.json)
    return 0


def _dispatch(
    args: argparse.Namespace, paths: TwinPaths, resources: ResourceCatalog
) -> dict[str, object]:
    command = args.command
    if command == "contract":
        return render_contract(build_parser(), resources)
    if command == "doctor":
        return doctor_report(paths, resources)
    if command == "setup":
        links = (
            check_skill_links(paths, paths.root.parent, resources=resources)
            if args.check
            else install_skill(paths, resources, paths.root.parent)
        )
        return _link_report(links)
    if command == "uninstall":
        return _link_report(uninstall_skill(paths, paths.root.parent))
    service = _service(paths, resources, require_runtime=command == "run")
    repo_root = Path.cwd()
    if command == "start":
        return service.start(args.goal, repo_root, args.supervisor)
    if command == "run":
        return service.run(args.workspace, repo_root, args.supervisor)
    if command == "status":
        return service.status(args.workspace, repo_root)
    if command == "respond":
        return service.respond(args.workspace, repo_root, args.answer)
    if command == "handoff":
        return service.handoff(args.workspace, repo_root, args.from_route, args.to_route)
    payload = _read_payload(args.payload_file)
    if command == "submit-plan":
        return service.submit_plan(
            args.workspace, args.supervisor, args.state_revision, args.action_token, payload
        )
    if command == "submit-review":
        return service.submit_review(
            args.workspace, args.supervisor, args.state_revision, args.action_token, args.run_id, payload
        )
    raise ValueError(f"unknown command: {command}")


def _service(
    paths: TwinPaths, resources: ResourceCatalog, *, require_runtime: bool = False
) -> TwinService:
    if not require_runtime:
        return TwinService(WorkspaceStore(paths), resources=resources)
    config = load_runtime_config(paths.config)
    runtime = build_runtime(config)
    return TwinService(
        WorkspaceStore(paths),
        runtime=runtime,
        isolation=GitWorkspaceIsolation(),
        resources=resources,
        timeout_seconds=config.timeout_seconds,
        worker_provider=config.worker_provider,
        runtime_adapter=config.adapter,
        runtime_config_digest=config.digest,
        provider_contract_version=getattr(runtime, "contract_version", 1),
    )


def _paths_for_home() -> TwinPaths:
    return TwinPaths.for_home(Path.home())


def _resource_catalog() -> ResourceCatalog:
    return ResourceCatalog()


def _add_json_flag(parser: argparse.ArgumentParser, *, required: bool = False) -> None:
    parser.add_argument("--json", action="store_true", required=required)


def _add_submission_command(
    add_command: Callable[..., argparse.ArgumentParser],
    name: str,
    argv: list[str],
    help_text: str,
    *,
    output: dict[str, str],
    needs_run_id: bool = False,
) -> None:
    command = add_command(
        name, visibility="action-only", argv=argv, output=output,
        help_text=help_text,
    )
    command.add_argument("--workspace", required=True)
    command.add_argument("--supervisor", required=True)
    command.add_argument("--state-revision", type=int, required=True)
    command.add_argument("--action-token", required=True)
    if needs_run_id:
        command.add_argument("--run-id", required=True)
    command.add_argument("--payload-file", type=_stdin_payload, required=True)
    _add_json_flag(command, required=True)


def _stdin_payload(value: str) -> str:
    if value != "-":
        raise argparse.ArgumentTypeError("payloads must be read from standard input (--payload-file -)")
    return value


def _read_payload(_: str) -> dict[str, object]:
    payload = json.loads(sys.stdin.read())
    if not isinstance(payload, dict):
        raise ValueError("submission payload must be a JSON object")
    return payload


def _emit(result: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return
    for key in sorted(result):
        value = result[key]
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
        print(f"{key}: {rendered}")


def _link_report(links: Sequence[LinkResult]) -> dict[str, object]:
    return {
        "ok": all(link.ok for link in links),
        "links": {link.name: link.as_dict() for link in links},
    }
