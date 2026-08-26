import subprocess
import shlex
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

from tests import smoke_installed


class SmokeInstalledTest(TestCase):
    def test_fake_git_fixture_executes_its_repository_contract(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            smoke_installed._install_fake_git(root, {})

            result = subprocess.run(
                [
                    sys.executable,
                    str(root / "bin" / "git"),
                    "-C",
                    str(repo),
                    "rev-parse",
                    "--show-toplevel",
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(Path(result.stdout.strip()), repo)

    def test_action_argv_preserves_a_leading_dash_token(self) -> None:
        argv = ["twin", "submit-plan", "--action-token=--generated-token"]
        self.assertEqual(
            smoke_installed._action_argv({
                "submit": {"argv": argv, "command": shlex.join(argv)},
            }, "submit"),
            argv,
        )

    def test_action_argv_rejects_command_drift(self) -> None:
        with self.assertRaisesRegex(AssertionError, "does not match argv"):
            smoke_installed._action_argv({
                "submit": {
                    "argv": ["twin", "submit-plan"],
                    "command": "twin submit-review",
                },
            }, "submit")
