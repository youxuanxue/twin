from __future__ import annotations

import sysconfig
from pathlib import Path


class ResourceCatalog:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else Path(sysconfig.get_path("data")) / "share" / "twin"

    def schema(self, name: str) -> Path:
        return self._require_file(
            self.root / "schemas" / f"twin.{name}.schema.json", "schema"
        )

    def persona(self, name: str) -> Path:
        return self._require_file(self.root / "personas" / f"{name}.md", "persona")

    def skill_dir(self) -> Path:
        return self._require_dir(self.root / "skills" / "twin", "skill directory")

    def template(self, name: str) -> Path:
        return self._require_file(
            self.root / "templates" / "workspace" / f"{name}.yaml", "template"
        )

    @staticmethod
    def _require_file(path: Path, resource_type: str) -> Path:
        if not path.is_file():
            raise FileNotFoundError(f"{resource_type} resource missing: {path}")
        return path

    @staticmethod
    def _require_dir(path: Path, resource_type: str) -> Path:
        if not path.is_dir():
            raise FileNotFoundError(f"{resource_type} resource missing: {path}")
        return path
