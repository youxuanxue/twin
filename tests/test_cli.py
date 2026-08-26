import json
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from twin.cli import build_parser, main, parser_help
from twin.contract import render_contract
from twin.paths import TwinPaths
from twin.resources import ResourceCatalog


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

    def test_contract_command_emits_json_without_provider_dependency(self) -> None:
        output = StringIO()
        with patch("sys.stdout", output):
            self.assertEqual(main(["contract", "--json"]), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["contract_version"], 1)
        self.assertIn("submit-review", payload["action_commands"])
        self.assertTrue(Path(payload["schema_paths"]["action"]).is_file())

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
