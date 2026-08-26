import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase


class SmokeStageIsolationTest(TestCase):
    def test_rejects_a_stage_parent_within_home_before_container_start(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            home.mkdir()
            wheel = root / "xuejiao_twin-0.1.0-py3-none-any.whl"
            wheel.write_bytes(b"not a wheel")
            env = dict(os.environ)
            env.update({
                "HOME": str(home),
                "TWIN_STAGE_PARENT": str(home / "staging"),
                "TWIN_WHEEL": str(wheel),
                "TWIN_CONTAINER_RUNTIME": "unsupported",
                "TWIN_REQUIRE_CONTAINER": "0",
            })

            result = subprocess.run(
                ["bash", "scripts/smoke-clean-home.sh"],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("TWIN_STAGE_PARENT must not be inside HOME", result.stderr)
