from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from twin.paths import TwinPaths
from twin.resources import ResourceCatalog


class ResourceCatalogTest(TestCase):
    def test_paths_are_home_scoped(self) -> None:
        with TemporaryDirectory() as raw:
            paths = TwinPaths.for_home(Path(raw))
            root = Path(raw).resolve() / ".twin"
            self.assertEqual(paths.root, root)
            self.assertEqual(paths.workspaces, root / "workspaces")
            self.assertEqual(paths.active_workspaces, root / "active-workspaces")
            self.assertEqual(paths.locks, root / "locks")

    def test_catalog_rejects_missing_installed_resource(self) -> None:
        with TemporaryDirectory() as raw:
            catalog = ResourceCatalog(Path(raw))
            with self.assertRaisesRegex(FileNotFoundError, "schema resource missing"):
                catalog.schema("goal")
