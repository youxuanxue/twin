import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from twin.doctor import doctor_report
from twin.paths import TwinPaths
from twin.resources import ResourceCatalog
from twin.setup import install_skill


class DoctorReportTest(TestCase):
    def test_base_health_ignores_missing_provider_binaries(self) -> None:
        with TemporaryDirectory() as raw:
            home = Path(raw) / "home"
            paths = TwinPaths.for_home(home)
            resources = ResourceCatalog(Path(__file__).resolve().parents[1])
            install_skill(paths, resources, home)

            with patch("twin.doctor.shutil.which", side_effect=lambda name: "/usr/bin/git" if name == "git" else None):
                report = doctor_report(paths, resources)

        expected_checks = {
            "python", "package_resources", "state_home", "installed_skill",
            "cursor_skill", "claude_skill",
            "codex_skill", "antigravity_skill", "git", "claude", "codex", "gemini",
            "runtime_configuration",
        }
        self.assertEqual(set(report["checks"]), expected_checks)
        self.assertTrue(report["ok"])
        self.assertTrue(report["checks"]["python"]["ok"])
        self.assertIn("Python", report["checks"]["python"]["detail"])
        self.assertFalse(report["checks"]["claude"]["ok"])
        self.assertFalse(report["checks"]["codex"]["ok"])
        self.assertFalse(report["checks"]["gemini"]["ok"])

    def test_doctor_validates_the_selected_runtime_configuration(self) -> None:
        with TemporaryDirectory() as raw:
            home = Path(raw) / "home"
            paths = TwinPaths.for_home(home)
            resources = ResourceCatalog(Path(__file__).resolve().parents[1])
            install_skill(paths, resources, home)
            paths.config.write_text(
                '[runtime]\nadapter = "local_cli"\nworker_provider = "codex"\n',
                encoding="utf-8",
            )
            with patch("twin.doctor.shutil.which", return_value="/usr/bin/git"):
                valid = doctor_report(paths, resources)
            self.assertTrue(valid["checks"]["runtime_configuration"]["ok"])
            self.assertIn("local_cli/codex", valid["checks"]["runtime_configuration"]["detail"])

            paths.config.write_text(
                '[runtime]\nadapter = "unknown"\nworker_provider = "codex"\n',
                encoding="utf-8",
            )
            with patch("twin.doctor.shutil.which", return_value="/usr/bin/git"):
                invalid = doctor_report(paths, resources)
            self.assertFalse(invalid["checks"]["runtime_configuration"]["ok"])
            self.assertIn("runtime.adapter", invalid["checks"]["runtime_configuration"]["detail"])

    def test_doctor_rejects_invalid_claude_permission_modes(self) -> None:
        with TemporaryDirectory() as raw:
            home = Path(raw) / "home"
            paths = TwinPaths.for_home(home)
            resources = ResourceCatalog(Path(__file__).resolve().parents[1])
            install_skill(paths, resources, home)

            for raw_mode in ("true", '"unsupported"'):
                with self.subTest(raw_mode=raw_mode):
                    paths.config.write_text(
                        '[runtime]\nadapter = "local_cli"\nworker_provider = "claude"\n'
                        '[local_cli]\nclaude_allowed_tools = ["Read"]\n'
                        'claude_max_budget_usd = 1\n'
                        f'claude_permission_mode = {raw_mode}\n',
                        encoding="utf-8",
                    )

                    with patch("twin.doctor.shutil.which", return_value="/usr/bin/git"):
                        report = doctor_report(paths, resources)

                    check = report["checks"]["runtime_configuration"]
                    self.assertFalse(check["ok"])
                    self.assertIn("claude_permission_mode", check["detail"])

    def test_doctor_requires_every_packaged_contract_schema(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            paths = TwinPaths.for_home(home)
            source = Path(__file__).resolve().parents[1]
            resource_root = root / "resources"
            for name in ("schemas", "personas", "skills", "templates"):
                shutil.copytree(source / name, resource_root / name)
            (resource_root / "schemas" / "twin.worker-submission.schema.json").unlink()
            resources = ResourceCatalog(resource_root)
            install_skill(paths, resources, home)

            with patch("twin.doctor.shutil.which", return_value="/usr/bin/git"):
                report = doctor_report(paths, resources)

            self.assertFalse(report["checks"]["package_resources"]["ok"])
            self.assertIn("worker-submission", report["checks"]["package_resources"]["detail"])
