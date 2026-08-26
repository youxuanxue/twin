import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase


class PreflightStaticGuardTest(TestCase):
    def test_rejects_a_scripts_twin_reference_before_running_tests(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            script = root / "scripts" / "preflight.sh"
            script.parent.mkdir()
            shutil.copy2(Path(__file__).resolve().parents[1] / "scripts" / "preflight.sh", script)
            (root / "README.md").write_text("# fixture\n", encoding="utf-8")
            (root / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
            (root / "docs").mkdir()
            (root / "skills").mkdir()
            runtime = root / "src" / "twin"
            runtime.mkdir(parents=True)
            (runtime / "forbidden.py").write_text(
                'LEGACY = "scripts.twin"\n', encoding="utf-8"
            )

            result = subprocess.run(
                ["bash", str(script)],
                cwd=root,
                env=dict(os.environ),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("forbidden standalone-runtime reference: scripts.twin", result.stderr)
