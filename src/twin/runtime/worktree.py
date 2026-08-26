from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


class GitWorkspaceIsolation:
    def __init__(self, *, git_binary: str = "git") -> None:
        self.git_binary = git_binary

    def prepare(self, repo_root: Path, workspace_id: str) -> Path:
        self._require_git()
        repo = self._repo_root(repo_root)
        safe_id = self._safe_workspace_id(workspace_id)
        branch = self._branch_name(safe_id)
        path = self._worktree_path(repo, safe_id)
        if path.exists():
            self._validate_existing_worktree(repo, path, branch)
            self._init_submodules(path)
            return path
        if self._branch_exists(repo, branch):
            raise RuntimeError(f"worktree branch already exists: {branch}")
        head = self._git(repo, "rev-parse", "--verify", "HEAD").stdout.strip()
        self._git(repo, "worktree", "add", "-b", branch, str(path), head)
        self._init_submodules(path)
        return path

    def cleanup(self, repo_root: Path, workspace_id: str) -> bool:
        self._require_git()
        repo = self._repo_root(repo_root)
        safe_id = self._safe_workspace_id(workspace_id)
        path = self._worktree_path(repo, safe_id)
        if not path.exists():
            return True
        self._validate_existing_worktree(repo, path, self._branch_name(safe_id))
        if not self._is_clean(path):
            return False
        self._git(repo, "worktree", "remove", "--force", str(path))
        branch = self._branch_name(safe_id)
        if self._branch_exists(repo, branch):
            self._git(repo, "branch", "-D", branch)
        return True

    def _require_git(self) -> None:
        if shutil.which(self.git_binary) is None:
            raise RuntimeError("git executable not found")

    def _repo_root(self, repo_root: Path) -> Path:
        repo = repo_root.expanduser().resolve()
        top = Path(self._git(repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
        if top != repo:
            raise RuntimeError(f"repo_root is not the git top-level: {repo}")
        return repo

    def _validate_existing_worktree(self, repo: Path, path: Path, branch: str) -> None:
        if not (path / ".git").exists():
            raise RuntimeError(f"worktree path exists but is not a git checkout: {path}")
        current_branch = self._git(path, "branch", "--show-current").stdout.strip()
        if current_branch != branch:
            raise RuntimeError(f"worktree branch mismatch: expected {branch}, found {current_branch}")
        repo_common = self._common_git_dir(repo)
        path_common = self._common_git_dir(path)
        if repo_common != path_common:
            raise RuntimeError("worktree repository mismatch")

    def _init_submodules(self, path: Path) -> None:
        self._git(path, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive")

    def _is_clean(self, path: Path) -> bool:
        status = self._git(
            path,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ).stdout
        if status:
            return False
        submodules = self._git(path, "submodule", "status", "--recursive").stdout
        for line in submodules.splitlines():
            if not line or line[0] == "-":
                continue
            parts = line.strip().split()
            if len(parts) < 2:
                continue
            submodule_path = path / parts[1]
            submodule_status = self._git(
                submodule_path,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignore-submodules=none",
            ).stdout
            if submodule_status:
                return False
        return True

    def _branch_exists(self, repo: Path, branch: str) -> bool:
        return self._git(repo, "rev-parse", "--verify", "--quiet", branch, check=False).returncode == 0

    def _common_git_dir(self, cwd: Path) -> Path:
        raw = Path(self._git(cwd, "rev-parse", "--git-common-dir").stdout.strip())
        if not raw.is_absolute():
            raw = cwd / raw
        return raw.resolve()

    def _git(self, cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                [self.git_binary, "-C", str(cwd), *args],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("git executable not found") from exc
        if check and result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git command failed")
        return result

    @staticmethod
    def _safe_workspace_id(workspace_id: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", workspace_id).strip("-")
        if not safe:
            raise RuntimeError("workspace_id is not usable for worktree isolation")
        return safe

    @staticmethod
    def _branch_name(safe_workspace_id: str) -> str:
        return f"twin/{safe_workspace_id}"

    @staticmethod
    def _worktree_path(repo: Path, safe_workspace_id: str) -> Path:
        return repo.parent / f"{repo.name}-twin-{safe_workspace_id}"
