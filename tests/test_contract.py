import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from twin.doctor import doctor_report
from twin.paths import TwinPaths
from twin.resources import ResourceCatalog


class DoctorReportTest(TestCase):
    def test_base_health_ignores_missing_provider_binaries(self) -> None:
        with TemporaryDirectory() as raw:
            home = Path(raw) / "home"
            paths = TwinPaths.for_home(home)
            resources = ResourceCatalog(Path(__file__).resolve().parents[1])
            for relative in (
                ".cursor/skills/twin",
                ".claude/skills/twin",
                ".codex/skills/twin",
                ".gemini/antigravity-cli/skills/twin",
            ):
                target = home / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(resources.skill_dir(), target)

            with patch("twin.doctor.shutil.which", side_effect=lambda name: "/usr/bin/git" if name == "git" else None):
                report = doctor_report(paths, resources)

        expected_checks = {
            "package_resources", "state_home", "cursor_skill", "claude_skill", "codex_skill",
            "antigravity_skill", "git", "claude", "codex", "gemini", "cao_configuration",
        }
        self.assertEqual(set(report["checks"]), expected_checks)
        self.assertTrue(report["ok"])
        self.assertFalse(report["checks"]["claude"]["ok"])
        self.assertFalse(report["checks"]["codex"]["ok"])
        self.assertFalse(report["checks"]["gemini"]["ok"])
