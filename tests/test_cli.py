import json
import shutil
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from twin.cli import build_parser, main, parser_help
from twin.contract import render_agent_integration, render_contract
from twin.domain.service import TwinService
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
        for hidden in ("submit-plan", "submit-instruction", "submit-review"):
            self.assertNotIn(hidden, help_text)

    def test_contract_includes_hidden_submission_commands(self) -> None:
        contract = render_contract(build_parser(), self.resources)
        self.assertEqual(contract["contract_version"], 1)
        self.assertIn("submit-plan", contract["action_commands"])
        self.assertIn("submit-instruction", contract["action_commands"])
        self.assertIn("submit-review", contract["action_commands"])

    def test_contract_describes_real_submission_argv_and_action_schema(self) -> None:
        contract = render_contract(build_parser(), self.resources)
        commands = contract["commands"]
        self.assertEqual(contract["package_version"], "0.1.0")
        self.assertEqual(commands["start"]["argv"], [
            "start", "<goal>", "--supervisor", "host/<provider>", "--json",
        ])
        self.assertEqual(commands["submit-instruction"]["argv"], [
            "submit-instruction", "--workspace", "<id>", "--supervisor", "host/<provider>",
            "--state-revision", "<int>", "--action-token", "<token>", "--run-id", "<id>",
            "--payload-file", "-", "--json",
        ])
        self.assertEqual(
            commands["start"]["output"],
            {"shape": "action", "schema_path": str(self.resources.schema("action"))},
        )

    def test_contract_describes_each_submission_result_shape(self) -> None:
        commands = render_contract(build_parser(), self.resources)["commands"]
        self.assertEqual(
            {name: commands[name]["output"] for name in (
                "submit-plan", "submit-instruction", "submit-review",
            )},
            {
                "submit-plan": {"shape": "workspace-result"},
                "submit-instruction": {
                    "shape": "action",
                    "schema_path": str(self.resources.schema("action")),
                },
                "submit-review": {"shape": "workspace-result"},
            },
        )

    def test_agent_integration_document_is_rendered_from_live_contract(self) -> None:
        document = render_agent_integration(build_parser(), self.resources)

        self.assertIn("Generated from `twin contract --json`", document)
        self.assertIn("## Commands", document)
        self.assertIn("`submit-review`", document)
        self.assertIn("`schemas/twin.action.schema.json`", document)
        self.assertNotIn(str(self.resources.root), document)
        generated = Path(__file__).resolve().parents[1] / "docs" / "agent-integration.md"
        self.assertEqual(generated.read_text(encoding="utf-8"), document)

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

    def test_submit_plan_validates_against_an_injected_installed_resource_root(self) -> None:
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
            result = service.submit_plan(
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

        self.assertEqual(result["status"], "ready")

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
                with patch("twin.cli.Path.cwd", return_value=repo):
                    with patch("sys.stdout", output):
                        self.assertEqual(main([
                            "start", "ship focused CLI", "--supervisor", "host/codex", "--json",
                        ]), 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["action"], "author_plan")
            self.assertEqual(payload["supervisor_route"], "host/codex")
