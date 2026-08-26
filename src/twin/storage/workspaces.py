from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from twin.paths import TwinPaths
from twin.storage.atomic import write_bytes, write_text
from twin.storage.events import append_jsonl, event_record, now_utc
from twin.storage.locks import exclusive_lock
from twin.yaml_codec import dump_yaml


_TRANSACTION_ID = re.compile(r"[0-9a-f]{32}")
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@dataclass
class _TransactionNamespace:
    workspace_fd: int
    root_fd: int | None
    transaction_fd: int | None
    transaction_id: str
    root_identity: tuple[int, int]
    transaction_identity: tuple[int, int]

    def close(self) -> None:
        for descriptor in (self.transaction_fd, self.root_fd, self.workspace_fd):
            if descriptor is not None:
                os.close(descriptor)


class WorkspaceStore:
    def __init__(self, paths: TwinPaths) -> None:
        self.paths = paths

    def create(self, request: str, repo_root: Path, route: str) -> str:
        canonical_repo = repo_root.expanduser().resolve()
        if not canonical_repo.is_dir():
            raise ValueError(f"target repository does not exist: {canonical_repo}")
        workspace_id = self._new_workspace_id(request)
        workspace = self.paths.workspaces / workspace_id
        workspace.mkdir(parents=True, exist_ok=False)
        (workspace / "artifacts").mkdir()
        state: dict[str, object] = {
            "schema_version": 1,
            "workspace_id": workspace_id,
            "status": "awaiting_plan",
            "state_revision": 0,
            "supervisor_route": route,
            "pending_action": None,
            "current_run_id": None,
            "current_item_id": None,
            "terminal_summary": None,
        }
        self._write_json(workspace / "meta.json", {
            "schema_version": 1,
            "workspace_id": workspace_id,
            "created_at": now_utc(),
            "repo_root": str(canonical_repo),
        })
        goal_text = request.strip() or "Untitled goal"
        dump_yaml(workspace / "goal.yaml", {
            "schema_version": 1,
            "id": workspace_id,
            "one_liner": goal_text,
            "core_goal": goal_text,
            "acceptance_criteria": [],
            "non_goals": [],
        })
        dump_yaml(workspace / "plan.yaml", {
            "schema_version": 1,
            "goal_id": workspace_id,
            "items": [],
            "verification": [],
        })
        self._write_json(workspace / "state.json", state)
        pointer = self.paths.active_workspaces / self._project_key(canonical_repo)
        write_text(pointer, workspace_id + "\n")
        self.append_event(workspace, {"event": "workspace_created", "details": {}})
        return workspace_id

    def resolve(self, ref: str | None, project_root: Path) -> Path:
        if ref is None:
            pointer = self.paths.active_workspaces / self._project_key(project_root.expanduser().resolve())
            if not pointer.is_file():
                raise ValueError("workspace is required")
            ref = pointer.read_text(encoding="utf-8").strip()
            if not ref:
                raise ValueError("active workspace pointer is empty")
        candidate = Path(ref).expanduser()
        if candidate.is_absolute():
            workspace = candidate.resolve()
        else:
            if candidate.name != ref or not ref:
                raise ValueError("workspace reference must be an ID")
            workspace = (self.paths.workspaces / ref).resolve()
        self._require_workspace_path(workspace)
        if not workspace.is_dir():
            raise ValueError(f"workspace does not exist: {workspace}")
        return self._display_path(workspace)

    def load_state(self, workspace: Path) -> dict[str, object]:
        workspace = self._workspace_path(workspace)
        with self._lock(workspace):
            self._recover_locked(workspace)
            value = self._load_state_unlocked(workspace)
        if not isinstance(value, dict):
            raise ValueError("state must be an object")
        return value

    def replace_state(
        self, workspace: Path, expected_revision: int, value: dict[str, object]
    ) -> None:
        workspace = self._workspace_path(workspace)
        self.commit_action(
            workspace, expected_revision, value,
            documents={}, artifacts={}, event=None, validate_current=lambda current: None,
        )

    def append_event(self, workspace: Path, event: dict[str, object]) -> None:
        workspace = self._workspace_path(workspace)
        with self._lock(workspace):
            self._recover_locked(workspace)
            self._append_event_locked(workspace, event)

    def write_artifact(
        self, workspace: Path, relative: str, body: bytes
    ) -> dict[str, object]:
        workspace = self._workspace_path(workspace)
        with self._lock(workspace):
            self._recover_locked(workspace)
            target = self._artifact_path(workspace, relative)
            write_bytes(target, body)
            metadata: dict[str, object] = {
                "relative": str(target.relative_to(workspace)),
                "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
            }
            self._append_event_locked(workspace, {"event": "artifact_written", "details": metadata})
        return metadata

    def commit_action(
        self,
        workspace: Path,
        expected_revision: int,
        value: dict[str, object],
        *,
        documents: dict[str, bytes],
        artifacts: dict[str, bytes],
        event: dict[str, object] | None,
        validate_current: Callable[[dict[str, object]], None],
    ) -> None:
        """Publish an action's documents, artifacts, state, and events under one workspace lock."""
        workspace = self._workspace_path(workspace)
        with self._lock(workspace):
            self._recover_locked(workspace)
            current = self._load_state_unlocked(workspace)
            revision = current.get("state_revision")
            if revision != expected_revision:
                raise ValueError(
                    f"state revision mismatch: expected {expected_revision}, found {revision}"
                )
            validate_current(current)
            if value.get("workspace_id") != current.get("workspace_id"):
                raise ValueError("state workspace_id mismatch")
            workspace_id = current.get("workspace_id")
            if not isinstance(workspace_id, str):
                raise ValueError("state workspace_id mismatch")
            targets: dict[Path, bytes] = {}
            for relative, body in documents.items():
                if relative not in {"goal.yaml", "plan.yaml"}:
                    raise ValueError("document path is not writable")
                targets[workspace / relative] = body
            artifact_metadata: list[dict[str, object]] = []
            for relative, body in artifacts.items():
                target = self._artifact_path(workspace, relative)
                targets[target] = body
                artifact_metadata.append({
                    "relative": str(target.relative_to(workspace)),
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "bytes": len(body),
                })
            next_revision = expected_revision + 1
            next_state = dict(value)
            next_state["state_revision"] = next_revision
            targets[workspace / "state.json"] = self._json_bytes(next_state)
            records: list[dict[str, object]] = [
                event_record(
                    workspace_id=workspace_id,
                    state_revision=next_revision,
                    event={"event": "state_replaced", "details": {}},
                ),
                *[
                    event_record(
                        workspace_id=workspace_id,
                        state_revision=next_revision,
                        event={"event": "artifact_written", "details": metadata},
                    )
                    for metadata in artifact_metadata
                ],
            ]
            if event is not None:
                records.append(
                    event_record(
                        workspace_id=workspace_id, state_revision=next_revision, event=event
                    )
                )
            prior_events = (workspace / "events.jsonl").read_bytes()
            targets[workspace / "events.jsonl"] = prior_events + b"".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
                for record in records
            )
            previous = {target: target.read_bytes() if target.exists() else None for target in targets}
            transaction_id = secrets.token_hex(16)
            journal = workspace / ".transaction.json"
            created_directories = self._missing_parent_dirs(workspace, targets)
            namespace = self._create_transaction_namespace(workspace, transaction_id)
            staged: dict[Path, str] = {}
            try:
                self._write_json(
                    journal,
                    self._transaction_journal(
                        namespace, workspace, previous, created_directories
                    ),
                )
                for directory in created_directories:
                    directory.mkdir(parents=True, exist_ok=True)
                for index, (target, body) in enumerate(targets.items()):
                    staged[target] = self._stage_bytes(
                        namespace.transaction_fd, f"{index}.stage", body
                    )
                self._assert_transaction_namespace(workspace, namespace)
                self._publish_staged(
                    staged, previous, workspace / "state.json", namespace.transaction_fd
                )
            except BaseException:
                if journal.is_file():
                    self._recover_locked(workspace)
                else:
                    self._cleanup_transaction_namespace(namespace, set())
                raise
            else:
                self._cleanup_transaction_namespace(namespace, set(staged.values()))
                journal.unlink(missing_ok=True)
            finally:
                namespace.close()

    def _load_state_unlocked(self, workspace: Path) -> dict[str, object]:
        value = self._read_json(workspace / "state.json")
        if not isinstance(value, dict):
            raise ValueError("state must be an object")
        return value

    def _append_event_locked(self, workspace: Path, event: dict[str, object]) -> None:
        state = self._load_state_unlocked(workspace)
        workspace_id = state.get("workspace_id")
        revision = state.get("state_revision")
        if not isinstance(workspace_id, str) or not isinstance(revision, int):
            raise ValueError("invalid workspace state")
        append_jsonl(
            workspace / "events.jsonl",
            event_record(workspace_id=workspace_id, state_revision=revision, event=event),
        )

    def _transaction_journal(
        self,
        namespace: _TransactionNamespace,
        workspace: Path,
        previous: dict[Path, bytes | None],
        created_directories: list[Path],
    ) -> dict[str, object]:
        return {
            "schema_version": 2,
            "transaction_id": namespace.transaction_id,
            "transaction_root_identity": {
                "device": namespace.root_identity[0],
                "inode": namespace.root_identity[1],
            },
            "transaction_identity": {
                "device": namespace.transaction_identity[0],
                "inode": namespace.transaction_identity[1],
            },
            "created_directories": [
                str(directory.relative_to(workspace)) for directory in created_directories
            ],
            "targets": [
                {
                    "relative": str(target.relative_to(workspace)),
                    "before": None if body is None else base64.b64encode(body).decode("ascii"),
                }
                for target, body in previous.items()
            ],
        }

    def _recover_locked(self, workspace: Path) -> None:
        journal = workspace / ".transaction.json"
        try:
            journal_status = os.stat(journal, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(journal_status.st_mode):
            raise ValueError("invalid transaction journal")
        namespace: _TransactionNamespace | None = None
        try:
            value = self._read_json(journal)
            transaction_id = value.get("transaction_id") if isinstance(value, dict) else None
            targets = value.get("targets") if isinstance(value, dict) else None
            created = value.get("created_directories", []) if isinstance(value, dict) else None
            root_identity = self._journal_identity(value, "transaction_root_identity")
            transaction_identity = self._journal_identity(value, "transaction_identity")
            if (
                value.get("schema_version") != 2
                or not isinstance(transaction_id, str)
                or not transaction_id
                or not isinstance(targets, list)
                or not isinstance(created, list)
            ):
                raise ValueError("invalid transaction journal")
            namespace = self._open_transaction_namespace(
                workspace, transaction_id, root_identity, transaction_identity
            )
            snapshots: list[tuple[Path, bytes | None]] = []
            for entry in targets:
                if not isinstance(entry, dict):
                    raise ValueError("invalid transaction journal")
                relative = entry.get("relative")
                before = entry.get("before")
                if not isinstance(relative, str) or before is not None and not isinstance(before, str):
                    raise ValueError("invalid transaction journal")
                target = self._transaction_target(workspace, relative)
                body = None if before is None else base64.b64decode(before.encode("ascii"), validate=True)
                snapshots.append((target, body))
            created_directories = [
                self._transaction_directory(workspace, relative)
                for relative in created
                if isinstance(relative, str)
            ]
            if len(created_directories) != len(created):
                raise ValueError("invalid transaction journal")
            expected_stages = {f"{index}.stage" for index in range(len(targets))}
            self._validate_transaction_namespace_contents(namespace, expected_stages)
        except (OSError, ValueError, TypeError) as exc:
            if namespace is not None:
                namespace.close()
            raise ValueError("invalid transaction journal") from exc
        try:
            for target, body in snapshots:
                if body is None:
                    target.unlink(missing_ok=True)
                else:
                    write_bytes(target, body)
            self._cleanup_created_directories(created_directories)
            self._cleanup_transaction_namespace(namespace, expected_stages)
            journal.unlink(missing_ok=True)
        finally:
            namespace.close()

    @staticmethod
    def _journal_identity(value: object, key: str) -> tuple[int, int]:
        identity = value.get(key) if isinstance(value, dict) else None
        device = identity.get("device") if isinstance(identity, dict) else None
        inode = identity.get("inode") if isinstance(identity, dict) else None
        if not isinstance(device, int) or not isinstance(inode, int):
            raise ValueError("invalid transaction journal")
        return device, inode

    def _create_transaction_namespace(
        self, workspace: Path, transaction_id: str
    ) -> _TransactionNamespace:
        if _TRANSACTION_ID.fullmatch(transaction_id) is None:
            raise ValueError("invalid transaction ID")
        workspace_fd = self._open_workspace_directory(workspace)
        root_fd: int | None = None
        transaction_fd: int | None = None
        try:
            try:
                os.mkdir(".transactions", 0o700, dir_fd=workspace_fd)
            except FileExistsError as exc:
                raise ValueError("unsafe transaction staging namespace") from exc
            root_fd = self._open_directory_entry(workspace_fd, ".transactions")
            try:
                os.mkdir(transaction_id, 0o700, dir_fd=root_fd)
            except FileExistsError as exc:
                raise ValueError("unsafe transaction staging namespace") from exc
            transaction_fd = self._open_directory_entry(root_fd, transaction_id)
            namespace = _TransactionNamespace(
                workspace_fd=workspace_fd,
                root_fd=root_fd,
                transaction_fd=transaction_fd,
                transaction_id=transaction_id,
                root_identity=self._descriptor_identity(root_fd),
                transaction_identity=self._descriptor_identity(transaction_fd),
            )
            self._assert_transaction_namespace(workspace, namespace)
            return namespace
        except BaseException:
            for descriptor in (transaction_fd, root_fd, workspace_fd):
                if descriptor is not None:
                    os.close(descriptor)
            raise

    def _open_transaction_namespace(
        self,
        workspace: Path,
        transaction_id: str,
        root_identity: tuple[int, int],
        transaction_identity: tuple[int, int],
    ) -> _TransactionNamespace:
        if _TRANSACTION_ID.fullmatch(transaction_id) is None:
            raise ValueError("invalid transaction journal")
        workspace_fd = self._open_workspace_directory(workspace)
        root_fd: int | None = None
        transaction_fd: int | None = None
        try:
            try:
                root_fd = self._open_directory_entry(workspace_fd, ".transactions")
            except FileNotFoundError:
                return _TransactionNamespace(
                    workspace_fd=workspace_fd,
                    root_fd=None,
                    transaction_fd=None,
                    transaction_id=transaction_id,
                    root_identity=root_identity,
                    transaction_identity=transaction_identity,
                )
            if self._descriptor_identity(root_fd) != root_identity:
                raise ValueError("invalid transaction journal")
            try:
                transaction_fd = self._open_directory_entry(root_fd, transaction_id)
            except FileNotFoundError:
                transaction_fd = None
            if (
                transaction_fd is not None
                and self._descriptor_identity(transaction_fd) != transaction_identity
            ):
                raise ValueError("invalid transaction journal")
            namespace = _TransactionNamespace(
                workspace_fd=workspace_fd,
                root_fd=root_fd,
                transaction_fd=transaction_fd,
                transaction_id=transaction_id,
                root_identity=root_identity,
                transaction_identity=transaction_identity,
            )
            self._assert_transaction_namespace(workspace, namespace)
            return namespace
        except BaseException:
            for descriptor in (transaction_fd, root_fd, workspace_fd):
                if descriptor is not None:
                    os.close(descriptor)
            raise

    @staticmethod
    def _open_workspace_directory(workspace: Path) -> int:
        descriptor = os.open(workspace, _DIRECTORY_OPEN_FLAGS)
        current = os.stat(workspace, follow_symlinks=False)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(current.st_mode)
            or WorkspaceStore._identity(current) != WorkspaceStore._identity(opened)
        ):
            os.close(descriptor)
            raise ValueError("unsafe workspace directory")
        return descriptor

    @staticmethod
    def _open_directory_entry(parent_fd: int, name: str) -> int:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(current.st_mode):
            raise ValueError("unsafe transaction staging namespace")
        descriptor = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if WorkspaceStore._identity(opened) != WorkspaceStore._identity(current):
            os.close(descriptor)
            raise ValueError("unsafe transaction staging namespace")
        return descriptor

    @staticmethod
    def _identity(status: os.stat_result) -> tuple[int, int]:
        return status.st_dev, status.st_ino

    @staticmethod
    def _descriptor_identity(descriptor: int) -> tuple[int, int]:
        return WorkspaceStore._identity(os.fstat(descriptor))

    @staticmethod
    def _entry_matches(parent_fd: int, name: str, descriptor: int) -> bool:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return (
            stat.S_ISDIR(current.st_mode)
            and WorkspaceStore._identity(current)
            == WorkspaceStore._descriptor_identity(descriptor)
        )

    def _assert_transaction_namespace(
        self, workspace: Path, namespace: _TransactionNamespace
    ) -> None:
        current_workspace = os.stat(workspace, follow_symlinks=False)
        if self._identity(current_workspace) != self._descriptor_identity(namespace.workspace_fd):
            raise ValueError("unsafe transaction staging namespace")
        if namespace.root_fd is None:
            try:
                os.stat(".transactions", dir_fd=namespace.workspace_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            raise ValueError("unsafe transaction staging namespace")
        if not self._entry_matches(
            namespace.workspace_fd, ".transactions", namespace.root_fd
        ):
            raise ValueError("unsafe transaction staging namespace")
        if namespace.transaction_fd is None:
            try:
                os.stat(
                    namespace.transaction_id,
                    dir_fd=namespace.root_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return
            raise ValueError("unsafe transaction staging namespace")
        if not self._entry_matches(
            namespace.root_fd, namespace.transaction_id, namespace.transaction_fd
        ):
            raise ValueError("unsafe transaction staging namespace")

    def _validate_transaction_namespace_contents(
        self, namespace: _TransactionNamespace, expected_stages: set[str]
    ) -> None:
        if namespace.root_fd is None:
            return
        root_entries = set(os.listdir(namespace.root_fd))
        expected_root_entries = (
            {namespace.transaction_id} if namespace.transaction_fd is not None else set()
        )
        if root_entries != expected_root_entries:
            raise ValueError("invalid transaction journal")
        if namespace.transaction_fd is None:
            return
        transaction_entries = set(os.listdir(namespace.transaction_fd))
        if not transaction_entries.issubset(expected_stages):
            raise ValueError("invalid transaction journal")
        for name in transaction_entries:
            entry = os.stat(name, dir_fd=namespace.transaction_fd, follow_symlinks=False)
            if not stat.S_ISREG(entry.st_mode):
                raise ValueError("invalid transaction journal")

    def _cleanup_transaction_namespace(
        self, namespace: _TransactionNamespace, expected_stages: set[str]
    ) -> None:
        self._validate_transaction_namespace_contents(namespace, expected_stages)
        if namespace.transaction_fd is not None:
            for name in os.listdir(namespace.transaction_fd):
                self._unlink_transaction_entry(namespace.transaction_fd, name)
            if os.listdir(namespace.transaction_fd):
                raise OSError("transaction staging cleanup incomplete")
            if not self._entry_matches(
                namespace.root_fd, namespace.transaction_id, namespace.transaction_fd
            ):
                raise OSError("transaction staging cleanup incomplete")
            self._remove_transaction_directory(namespace.root_fd, namespace.transaction_id)
        if namespace.root_fd is not None:
            if os.listdir(namespace.root_fd):
                raise OSError("transaction staging cleanup incomplete")
            if not self._entry_matches(
                namespace.workspace_fd, ".transactions", namespace.root_fd
            ):
                raise OSError("transaction staging cleanup incomplete")
            self._remove_transaction_directory(namespace.workspace_fd, ".transactions")
        try:
            os.stat(".transactions", dir_fd=namespace.workspace_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise OSError("transaction staging cleanup incomplete")

    @staticmethod
    def _unlink_transaction_entry(transaction_fd: int, name: str) -> None:
        os.unlink(name, dir_fd=transaction_fd)

    @staticmethod
    def _remove_transaction_directory(parent_fd: int, name: str) -> None:
        os.rmdir(name, dir_fd=parent_fd)

    def _transaction_target(self, workspace: Path, relative: str) -> Path:
        candidate = Path(relative)
        if not relative or candidate.is_absolute() or relative.startswith(".transactions/"):
            raise ValueError("invalid transaction journal")
        target = (workspace / candidate).resolve()
        try:
            rendered = target.relative_to(workspace)
        except ValueError as exc:
            raise ValueError("invalid transaction journal") from exc
        if rendered.parts and rendered.parts[0] in {".transaction.json", ".transactions"}:
            raise ValueError("invalid transaction journal")
        return target

    def _transaction_directory(self, workspace: Path, relative: str) -> Path:
        directory = self._transaction_target(workspace, relative)
        if directory == workspace or directory.name in {
            ".transaction.json", "goal.yaml", "plan.yaml", "state.json", "events.jsonl"
        }:
            raise ValueError("invalid transaction journal")
        return directory

    @staticmethod
    def _cleanup_created_directories(created_directories: list[Path]) -> None:
        for directory in reversed(created_directories):
            if directory.exists():
                directory.rmdir()
            if directory.exists() or directory.is_symlink():
                raise OSError(f"transaction-created directory cleanup incomplete: {directory}")

    @staticmethod
    def _missing_parent_dirs(workspace: Path, targets: dict[Path, bytes]) -> list[Path]:
        missing: set[Path] = set()
        for target in targets:
            parent = target.parent
            while parent != workspace and not parent.exists():
                missing.add(parent)
                parent = parent.parent
        return sorted(missing, key=lambda path: len(path.parts))

    def _new_workspace_id(self, request: str) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        slug = re.sub(r"[^a-z0-9]+", "-", request.lower()).strip("-")[:48] or "workspace"
        return f"{stamp}-{slug}-{secrets.token_hex(4)}"

    def _project_key(self, repo_root: Path) -> str:
        return hashlib.sha256(str(repo_root).encode("utf-8")).hexdigest()

    def _lock(self, workspace: Path):
        return exclusive_lock(self.paths.locks / f"{workspace.name}.lock")

    def _workspace_path(self, workspace: Path) -> Path:
        resolved = workspace.expanduser().resolve()
        self._require_workspace_path(resolved)
        if not resolved.is_dir():
            raise ValueError(f"workspace does not exist: {resolved}")
        return resolved

    def _require_workspace_path(self, path: Path) -> None:
        root = self.paths.workspaces.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"workspace path is outside store root: {path}") from exc

    def _artifact_path(self, workspace: Path, relative: str) -> Path:
        candidate = Path(relative)
        if not relative or candidate.is_absolute():
            raise ValueError("artifact path must be relative")
        target = (workspace / candidate).resolve()
        try:
            rendered = target.relative_to(workspace)
        except ValueError as exc:
            raise ValueError("artifact path escapes workspace") from exc
        if rendered.parts and rendered.parts[0] in {".transaction.json", ".transactions"}:
            raise ValueError("artifact path is reserved")
        if target == workspace or target.name in {
            "meta.json", "goal.yaml", "plan.yaml", "state.json", "events.jsonl"
        }:
            raise ValueError("artifact path is reserved")
        return target

    @staticmethod
    def _display_path(path: Path) -> Path:
        """Preserve macOS's conventional /var spelling for user-facing refs."""
        rendered = str(path)
        if rendered.startswith("/private/var/"):
            return Path(rendered[len("/private"):])
        return path

    @staticmethod
    def _read_json(path: Path) -> object:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        write_text(path, WorkspaceStore._json_bytes(value).decode("utf-8"))

    @staticmethod
    def _json_bytes(value: object) -> bytes:
        return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")

    @staticmethod
    def _stage_bytes(transaction_fd: int | None, name: str, body: bytes) -> str:
        if transaction_fd is None:
            raise ValueError("unsafe transaction staging namespace")
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=transaction_fd,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                os.unlink(name, dir_fd=transaction_fd)
            except FileNotFoundError:
                pass
            raise
        return name

    @staticmethod
    def _publish_staged(
        staged: dict[Path, str],
        previous: dict[Path, bytes | None],
        state_path: Path,
        transaction_fd: int | None,
    ) -> None:
        if transaction_fd is None:
            raise ValueError("unsafe transaction staging namespace")
        ordered = [target for target in staged if target != state_path] + [state_path]
        for target in ordered:
            os.replace(staged[target], target, src_dir_fd=transaction_fd)
