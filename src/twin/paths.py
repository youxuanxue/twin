from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TwinPaths:
    root: Path
    workspaces: Path
    active_workspaces: Path
    locks: Path
    installed_skills: Path
    config: Path

    @classmethod
    def for_home(cls, home: Path) -> "TwinPaths":
        root = home.expanduser().resolve() / ".twin"
        return cls(
            root=root,
            workspaces=root / "workspaces",
            active_workspaces=root / "active-workspaces",
            locks=root / "locks",
            installed_skills=root / "skills",
            config=root / "config.toml",
        )
