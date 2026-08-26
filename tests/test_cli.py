import json
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from twin.cli import build_parser, main, parser_help
from twin.contract import _package_version, render_agent_integration, render_contract
from twin.domain.service import TwinService
from twin.errors import WorkspaceBusyError
from twin.paths import TwinPaths
from twin.resources import ResourceCatalog
from twin.storage.workspaces import WorkspaceStore


class CliSurfaceTest(TestCase):
    def setUp(self) -> None:
        self.resources = ResourceCatalog(Path(__file__).resolve().parents[1])

    def test_public_help_is_small(self) -> None:
        help_text = parser_help(build_parser())
        for command in ("start", "run", "status", "respond", "handoff", "doctor", "contract"):
            self.assertIn(command, help_text)
        for removed in ("scaffold", "bootstrap", "research", "plan", "next", "watch", "worker-turn", "review-context"):
            self.assertNotIn(removed, help_text)
        for hidden in ("submit-plan", "submit-review"):
            self.assertNotIn(hidden, help_text)

    def test_contract_includes_hidden_submission_commands(self) -> None:
        contract = render_contract(build_parser(), self.resources)
        self.assertEqual(contract["contract_version"], 1)
        self.assertEqual(contract["action_commands"], ["submit-plan", "submit-review"])

    def test_contract_describes_real_submission_argv_and_action_schema(self) -> None:
        contract = render_contract(build_parser(), self.resources)
        commands = contract["commands"]
        self.assertEqual(contract["package_version"], "0.1.0")
        self.assertEqual(commands["start"]["argv"], [
            "start", "<goal>", "--supervisor", "host/<provider>", "--json",
        ])
        self.assertNotIn("submit-instruction", commands)
        self.assertEqual(
            commands["start"]["output"],
            {"shape": "action", "schema_path": str(self.resources.schema("action"))},
        )

    def test_contract_describes_each_submission_result_shape(self) -> None:
        commands = render_contract(build_parser(), self.resources)["commands"]
        self.assertEqual(
            {name: commands[name]["output"] for name in ("submit-plan", "submit-review")},
            {
                "submit-plan": {
                    "shape": "workspace-result",
                    "continuation_field": "next_command",
                },
                "submit-review": {
                    "shape": "workspace-result",
                    "continuation_field": "next_command",
                },
            },
        )

    def test_contract_exposes_every_live_schema(self) -> None:
        contract = render_contract(build_parser(), self.resources)
        self.assertEqual(set(contract["schema_paths"]), {
            "action", "event", "goal", "meta", "plan", "run-evidence",
            "run-request", "run-result", "state", "worker-submission",
        })

    def test_agent_integration_document_is_rendered_from_live_contract(self) -> None:
        document = render_agent_integration(build_parser(), self.resources)

        self.assertIn("Generated from `twin contract --json`", document)
        self.assertIn("## Commands", document)
        self.assertIn("`submit-review`", document)
        self.assertIn("`schemas/twin.action.schema.json`", document)
        self.assertIn("continue from the returned workspace result", document)
        self.assertIn("`next_command.argv`", document)
        self.assertNotIn(str(self.resources.root), document)
        generated = Path(__file__).resolve().parents[1] / "docs" / "agent-integration.md"
        self.assertEqual(generated.read_text(encoding="utf-8"), document)

    def test_agent_contract_export_script_checks_the_generated_document(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "export_agent_contract.py"
        result = subprocess.run(
            [sys.executable, str(script), "--check"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_mandatory_json_commands_reject_missing_json_flag(self) -> None:
        parser = build_parser()
        cases = (
            ["start", "ship", "--supervisor", "host/codex"],
            ["run", "--supervisor", "host/codex"],
            ["handoff", "workspace", "--from", "host/codex", "--to", "host/claude"],
            ["contract"],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                with redirect_stderr(StringIO()):
                    with self.assertRaises(SystemExit) as failure:
                        parser.parse_args(argv)
                self.assertEqual(failure.exception.code, 2)

    def test_optional_doctor_without_json_uses_text_renderer(self) -> None:
        with TemporaryDirectory() as raw:
            output = StringIO()
            with patch("twin.cli._paths_for_home", return_value=TwinPaths.for_home(Path(raw) / "home")):
                with patch("sys.stdout", output):
                    self.assertEqual(main(["doctor"]), 0)
        self.assertTrue(output.getvalue().startswith("checks:"))

    def test_workspace_busy_error_is_reported_without_a_traceback(self) -> None:
        error = StringIO()
        with patch(
            "twin.cli._dispatch",
            side_effect=WorkspaceBusyError("workspace is busy: worker-runtime"),
        ):
            with redirect_stderr(error):
                self.assertEqual(main(["status"]), 1)

        self.assertEqual(error.getvalue(), "workspace is busy: worker-runtime\n")

    def test_submit_plan_rejects_an_untouched_draft_at_the_ready_boundary(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            installed_root = root / "installed" / "share" / "twin"
            source_root = Path(__file__).resolve().parents[1]
            for name in ("schemas", "personas", "templates"):
                shutil.copytree(source_root / name, installed_root / name)
            shutil.copytree(source_root / "skills", installed_root / "skills")
            repo = root / "repo"
            repo.mkdir()
            service = TwinService(
                WorkspaceStore(TwinPaths.for_home(root / "home")),
                resources=ResourceCatalog(installed_root),
            )

            action = service.start("ship", repo, "host/codex")
            with self.assertRaisesRegex(ValueError, "at least one acceptance criterion"):
                service.submit_plan(
                    action["workspace"], "host/codex", action["state_revision"], action["action_token"],
                    {
                    "goal": {
                        "schema_version": 1,
                        "id": "assigned-by-service",
                        "one_liner": "Ship",
                        "core_goal": "Ship safely",
                        "acceptance_criteria": [],
                        "non_goals": [],
                    },
                    "plan": {
                        "schema_version": 1,
                        "goal_id": "assigned-by-service",
                        "items": [],
                        "verification": [],
                    },
                    },
                )

    def test_source_tree_contract_uses_an_explicit_development_version(self) -> None:
        with patch("twin.contract.version", side_effect=PackageNotFoundError):
            self.assertEqual(_package_version(), "0+development")

    def test_contract_command_emits_json_without_provider_dependency(self) -> None:
        output = StringIO()
        with patch("twin.cli._resource_catalog", return_value=self.resources):
            with patch("sys.stdout", output):
                self.assertEqual(main(["contract", "--json"]), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["contract_version"], 1)
        self.assertIn("submit-review", payload["action_commands"])
        self.assertTrue(Path(payload["schema_paths"]["action"]).is_file())

    def test_resource_catalog_never_falls_back_to_a_source_checkout(self) -> None:
        missing = ResourceCatalog(Path("/missing-installed-resources"))
        with patch("twin.cli.ResourceCatalog", return_value=missing) as catalog:
            from twin.cli import _resource_catalog

            self.assertIs(_resource_catalog(), missing)
        catalog.assert_called_once_with()

    def test_start_emits_an_author_plan_action(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            output = StringIO()
            with patch("twin.cli._paths_for_home", return_value=TwinPaths.for_home(root / "home")):
                with patch("twin.cli._resource_catalog", return_value=self.resources):
                    with patch("twin.cli.Path.cwd", return_value=repo):
                        with patch("sys.stdout", output):
                            self.assertEqual(main([
                                "start", "ship focused CLI", "--supervisor", "host/codex", "--json",
                            ]), 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["action"], "author_plan")
            self.assertEqual(payload["supervisor_route"], "host/codex")
