import json
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from twin.cli import main
from twin.doctor import doctor_report
from twin.paths import TwinPaths
from twin.resources import ResourceCatalog
from twin.setup import check_skill_links, install_skill, uninstall_skill


class SetupOwnershipTest(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.paths = TwinPaths.for_home(self.home)
        self.resource_root = self.root / "resources"
        source_root = Path(__file__).resolve().parents[1]
        shutil.copytree(source_root / "skills", self.resource_root / "skills")
        self.resources = ResourceCatalog(self.resource_root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_setup_refuses_foreign_cursor_entry(self) -> None:
        foreign = self.home / ".cursor" / "skills" / "twin"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("user owned", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "refusing to replace user-owned"):
            install_skill(self.paths, self.resources, self.home)

    def test_setup_copies_skill_then_creates_direct_host_links(self) -> None:
        install_skill(self.paths, self.resources, self.home)

        installed = self.home / ".twin" / "skills" / "twin"
        self.assertTrue(installed.is_dir())
        self.assertFalse(installed.is_symlink())
        self.assertEqual(
            (installed / "SKILL.md").read_text(encoding="utf-8"),
            (self.resource_root / "skills" / "twin" / "SKILL.md").read_text(encoding="utf-8"),
        )
        for relative in (
            ".cursor/skills/twin",
            ".codex/skills/twin",
            ".gemini/antigravity-cli/skills/twin",
        ):
            target = self.home / relative
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.resolve(), installed.resolve())

        claude_skills = self.home / ".claude" / "skills"
        self.assertTrue(claude_skills.is_symlink())
        self.assertEqual(
            claude_skills.resolve(), (self.home / ".cursor" / "skills").resolve()
        )

    def test_setup_rejects_legacy_cursor_root_link(self) -> None:
        cursor = self.home / ".cursor"
        cursor.mkdir(parents=True)
        legacy_registry = self.root / "legacy-skills"
        legacy_registry.mkdir()
        (cursor / "skills").symlink_to(legacy_registry, target_is_directory=True)

        with self.assertRaisesRegex(
            ValueError, "complete the additive-registry cutover first"
        ):
            install_skill(self.paths, self.resources, self.home)

    def test_uninstall_removes_only_twin_owned_links(self) -> None:
        install_skill(self.paths, self.resources, self.home)
        foreign = self.home / ".codex" / "skills" / "other"
        foreign.parent.mkdir(parents=True, exist_ok=True)
        foreign.symlink_to(self.home / "other-skill")

        uninstall_skill(self.paths, self.home)

        self.assertTrue(foreign.is_symlink())
        self.assertFalse((self.home / ".codex" / "skills" / "twin").exists())
        self.assertFalse((self.home / ".twin" / "skills" / "twin").exists())

    def test_uninstall_is_idempotent_and_preserves_foreign_twin_link(self) -> None:
        foreign = self.home / ".codex" / "skills" / "twin"
        foreign.parent.mkdir(parents=True)
        foreign.symlink_to(self.root / "foreign-twin")

        uninstall_skill(self.paths, self.home)
        uninstall_skill(self.paths, self.home)

        self.assertTrue(foreign.is_symlink())
        self.assertEqual(foreign.readlink(), self.root / "foreign-twin")

    def test_setup_check_and_doctor_report_use_installed_skill_links(self) -> None:
        install_skill(self.paths, self.resources, self.home)

        links = check_skill_links(self.paths, self.home, resources=self.resources)
        self.assertTrue(all(link.ok for link in links))

        with patch("twin.doctor.shutil.which", return_value="/usr/bin/git"):
            report = doctor_report(self.paths, self.resources)
        self.assertTrue(report["checks"]["cursor_skill"]["ok"])
        self.assertTrue(report["checks"]["claude_skill"]["ok"])
        self.assertTrue(report["checks"]["codex_skill"]["ok"])
        self.assertTrue(report["checks"]["antigravity_skill"]["ok"])

        output = StringIO()
        with patch("twin.cli._paths_for_home", return_value=self.paths):
            with patch("twin.cli._resource_catalog", return_value=self.resources):
                with redirect_stdout(output):
                    self.assertEqual(main(["setup", "--check", "--json"]), 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(set(payload["links"]), {
            "installed_skill", "cursor_skill", "claude_skill", "codex_skill",
            "antigravity_skill",
        })

    def test_setup_check_rejects_legacy_cursor_registry_link(self) -> None:
        legacy_registry = self._replace_cursor_registry_with_legacy_link()

        output = StringIO()
        with patch("twin.cli._paths_for_home", return_value=self.paths):
            with patch("twin.cli._resource_catalog", return_value=self.resources):
                with redirect_stdout(output):
                    self.assertEqual(main(["setup", "--check", "--json"]), 0)

        payload = json.loads(output.getvalue())
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["links"]["cursor_skill"]["ok"])
        self.assertIn(
            "complete the additive-registry cutover first",
            payload["links"]["cursor_skill"]["detail"],
        )
        self.assertTrue((self.home / ".cursor" / "skills").is_symlink())
        self.assertEqual(
            (self.home / ".cursor" / "skills").resolve(), legacy_registry.resolve()
        )

    def test_doctor_rejects_legacy_cursor_registry_link(self) -> None:
        legacy_registry = self._replace_cursor_registry_with_legacy_link()

        output = StringIO()
        with patch("twin.cli._paths_for_home", return_value=self.paths):
            with patch("twin.cli._resource_catalog", return_value=self.resources):
                with patch("twin.doctor.shutil.which", return_value="/usr/bin/git"):
                    with redirect_stdout(output):
                        self.assertEqual(main(["doctor", "--json"]), 0)

        payload = json.loads(output.getvalue())
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["checks"]["cursor_skill"]["ok"])
        self.assertIn(
            "complete the additive-registry cutover first",
            payload["checks"]["cursor_skill"]["detail"],
        )
        self.assertTrue((self.home / ".cursor" / "skills").is_symlink())
        self.assertEqual(
            (self.home / ".cursor" / "skills").resolve(), legacy_registry.resolve()
        )

    def test_setup_check_and_doctor_reject_installed_skill_tree_drift(self) -> None:
        install_skill(self.paths, self.resources, self.home)
        installed = self.paths.installed_skills / "twin"
        cases = ("changed", "missing", "extra", "symlink")
        for case in cases:
            with self.subTest(case=case):
                install_skill(self.paths, self.resources, self.home)
                if case == "changed":
                    (installed / "SKILL.md").write_text("changed\n", encoding="utf-8")
                elif case == "missing":
                    (installed / "agents" / "openai.yaml").unlink()
                elif case == "extra":
                    (installed / "extra.txt").write_text("extra\n", encoding="utf-8")
                else:
                    (installed / "SKILL.md").unlink()
                    (installed / "SKILL.md").symlink_to(self.root / "foreign-skill")
                try:
                    links = check_skill_links(
                        self.paths, self.home, resources=self.resources
                    )
                except TypeError as exc:
                    self.fail(f"setup check cannot compare packaged resources: {exc}")
                by_name = {link.name: link for link in links}
                self.assertFalse(by_name["installed_skill"].ok)
                self.assertIn("drift", by_name["installed_skill"].detail)
                with patch("twin.doctor.shutil.which", return_value="/usr/bin/git"):
                    report = doctor_report(self.paths, self.resources)
                self.assertFalse(report["ok"])
                self.assertFalse(report["checks"]["installed_skill"]["ok"])

    def test_checked_in_skill_manifest_matches_the_packaged_tree(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "generate-skill-manifest.py"
        result = subprocess.run(
            [sys.executable, str(script), "--check"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def _replace_cursor_registry_with_legacy_link(self) -> Path:
        install_skill(self.paths, self.resources, self.home)
        cursor_skills = self.home / ".cursor" / "skills"
        legacy_registry = self.root / "legacy-skills"
        cursor_skills.rename(legacy_registry)
        cursor_skills.symlink_to(legacy_registry, target_is_directory=True)
        return legacy_registry
