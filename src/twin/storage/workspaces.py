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

from twin.domain.integrity import WorkspaceSnapshot, validate_workspace_integrity
from twin.paths import TwinPaths
from twin.resources import ResourceCatalog
from twin.storage.atomic import write_text
from twin.storage.events import append_jsonl, event_record, now_utc
from twin.storage.locks import exclusive_lock
from twin.yaml_codec import dump_yaml


_TRANSACTION_ID = re.compile(r"[0-9a-f]{32}")
_TRANSACTION_STAGE = re.compile(r"\.twin-txn-([0-9a-f]{32})-([0-9]+)\.stage")
_TRANSACTION_STAGE_PREFIX = ".twin-txn-"
_TRANSACTION_JOURNAL = ".transaction.json"
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@dataclass
class _StagedFile:
    name: str
    descriptor: int
    identity: tuple[int, int]

    def close(self) -> None:
        os.close(self.descriptor)


class WorkspaceStore:
    def __init__(self, paths: TwinPaths, resources: ResourceCatalog | None = None) -> None:
        self.paths = paths
        self.integrity_resources = resources

    def bind_integrity(self, resources: ResourceCatalog) -> None:
        if (
            self.integrity_resources is not None
            and self.integrity_resources.root.resolve() != resources.root.resolve()
        ):
            raise ValueError("workspace store resource catalog mismatch")
        self.integrity_resources = resources

    def create(self, request: str, repo_root: Path, route: str) -> str:
        canonical_repo = repo_root.expanduser().resolve()
        if not canonical_repo.is_dir():
            raise ValueError(f"target repository does not exist: {canonical_repo}")
        workspace_id = self._new_workspace_id(request)
        repository_identity = self._project_key(canonical_repo)
        workspace = self.paths.workspaces / workspace_id
        workspace.mkdir(parents=True, exist_ok=False)
        (workspace / "artifacts").mkdir()
        state: dict[str, object] = {
            "schema_version": 1,
            "workspace_id": workspace_id,
            "status": "awaiting_plan",
            "state_revision": 0,
            "supervisor_route": route,
            "repository_identity": repository_identity,
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
            "repository_identity": repository_identity,
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
        state_body = self._json_bytes(state)
        write_text(workspace / "state.json", state_body.decode("utf-8"))
        pointer = self.paths.active_workspaces / self._project_key(canonical_repo)
        write_text(pointer, workspace_id + "\n")
        append_jsonl(
            workspace / "events.jsonl",
            event_record(
                workspace_id=workspace_id,
                state_revision=0,
                event={
                    "event": "workspace_created",
                    "details": {
                        "repository_identity": repository_identity,
                        "sha256": hashlib.sha256(state_body).hexdigest(),
                    },
                },
            ),
        )
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
        canonical_project = project_root.expanduser().resolve()
        with self._lock(workspace):
            self._recover_locked(workspace)
            snapshot = self._integrity_snapshot_locked(workspace)
            meta = snapshot.meta if snapshot is not None else self._read_json(workspace / "meta.json")
            recorded = meta.get("repo_root") if isinstance(meta, dict) else None
            if recorded != str(canonical_project):
                raise ValueError("workspace repository mismatch")
        return self._display_path(workspace)

    def resolve_submission(self, ref: str) -> Path:
        candidate = Path(ref).expanduser()
        if candidate.name != ref or not ref:
            raise ValueError("workspace reference must be an ID")
        workspace = (self.paths.workspaces / ref).resolve()
        self._require_workspace_path(workspace)
        if not workspace.is_dir():
            raise ValueError(f"workspace does not exist: {workspace}")
        with self._lock(workspace):
            self._recover_locked(workspace)
            self._integrity_snapshot_locked(workspace)
        return self._display_path(workspace)

    def load_state(self, workspace: Path) -> dict[str, object]:
        workspace = self._workspace_path(workspace)
        with self._lock(workspace):
            self._recover_locked(workspace)
            snapshot = self._integrity_snapshot_locked(workspace)
            value = snapshot.state if snapshot is not None else self._load_state_unlocked(workspace)
        if not isinstance(value, dict):
            raise ValueError("state must be an object")
        return value

    def inspect(self, workspace: Path, resources: ResourceCatalog) -> WorkspaceSnapshot:
        workspace = self._workspace_path(workspace)
        with self._lock(workspace):
            self._recover_locked(workspace)
            return validate_workspace_integrity(workspace, resources)

    def worker_runtime_lock(self, workspace: Path):
        workspace = self._workspace_path(workspace)
        return exclusive_lock(self.paths.locks / f"{workspace.name}.worker-runtime.lock")

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
            self._integrity_snapshot_locked(workspace)
            self._append_event_locked(workspace, event)

    def write_artifact(
        self, workspace: Path, relative: str, body: bytes
    ) -> dict[str, object]:
        workspace = self._workspace_path(workspace)
        target = self._artifact_path(workspace, relative)
        canonical_relative = str(target.relative_to(workspace))
        metadata: dict[str, object] = {
            "relative": canonical_relative,
            "sha256": hashlib.sha256(body).hexdigest(),
            "bytes": len(body),
        }
        state = self.load_state(workspace)
        revision = state.get("state_revision")
        if not isinstance(revision, int):
            raise ValueError("invalid state revision")
        self.commit_action(
            workspace,
            revision,
            state,
            documents={},
            artifacts={canonical_relative: body},
            event=None,
            validate_current=lambda current: None,
        )
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
            workspace_fd = self._open_workspace_directory(workspace)
            try:
                self._recover_locked(workspace, workspace_fd)
                self._integrity_snapshot_locked(workspace)
                self._commit_action_locked(
                    workspace,
                    workspace_fd,
                    expected_revision,
                    value,
                    documents,
                    artifacts,
                    event,
                    validate_current,
                )
            finally:
                os.close(workspace_fd)

    def _commit_action_locked(
        self,
        workspace: Path,
        workspace_fd: int,
        expected_revision: int,
        value: dict[str, object],
        documents: dict[str, bytes],
        artifacts: dict[str, bytes],
        event: dict[str, object] | None,
        validate_current: Callable[[dict[str, object]], None],
    ) -> None:
        current = self._read_json_entry(workspace_fd, "state.json")
        if not isinstance(current, dict):
            raise ValueError("state must be an object")
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
            if target in targets:
                raise ValueError(
                    f"duplicate artifact path: {target.relative_to(workspace)}"
                )
            if self._read_target_bytes(
                workspace_fd, str(target.relative_to(workspace))
            ) is not None:
                raise ValueError(f"artifact already exists: {relative}")
            targets[target] = body
            artifact_metadata.append({
                "relative": str(target.relative_to(workspace)),
                "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
            })
        next_revision = expected_revision + 1
        next_state = dict(value)
        next_state["state_revision"] = next_revision
        state_body = self._json_bytes(next_state)
        targets[workspace / "state.json"] = state_body
        records: list[dict[str, object]] = [
            event_record(
                workspace_id=workspace_id,
                state_revision=next_revision,
                event={
                    "event": "state_replaced",
                    "details": {"sha256": hashlib.sha256(state_body).hexdigest()},
                },
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
        prior_events = self._read_target_bytes(workspace_fd, "events.jsonl")
        if prior_events is None:
            raise ValueError("workspace event stream is missing")
        targets[workspace / "events.jsonl"] = prior_events + b"".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
            for record in records
        )
        transaction_id = secrets.token_hex(16)
        staged: dict[Path, _StagedFile] = {}
        journal_created = False
        previous = {
            target: self._read_target_bytes(
                workspace_fd, str(target.relative_to(workspace))
            )
            for target in targets
        }
        created_directories = self._missing_parent_dirs(workspace, targets)
        try:
            self._reject_unowned_stage_entries(workspace_fd)
            for index, (target, body) in enumerate(targets.items()):
                staged[target] = self._stage_bytes(
                    workspace_fd, self._stage_name(transaction_id, index), body
                )
            def mark_journal_created() -> None:
                nonlocal journal_created
                journal_created = True

            journal_identity = self._write_transaction_journal(
                workspace_fd,
                transaction_id,
                self._transaction_journal(
                    transaction_id,
                    self._descriptor_identity(workspace_fd),
                    workspace,
                    previous,
                    created_directories,
                    list(staged.values()),
                ),
                mark_journal_created=mark_journal_created,
            )
            self._publish_staged(
                staged, previous, workspace / "state.json", workspace_fd
            )
        except BaseException:
            if journal_created:
                self._recover_locked(workspace, workspace_fd)
            else:
                self._cleanup_staged_files(workspace_fd, list(staged.values()))
            raise
        else:
            self._cleanup_staged_files(workspace_fd, list(staged.values()))
            self._unlink_matching_entry(
                workspace_fd, _TRANSACTION_JOURNAL, journal_identity
            )
        finally:
            for stage in staged.values():
                stage.close()

    def _load_state_unlocked(self, workspace: Path) -> dict[str, object]:
        value = self._read_json(workspace / "state.json")
        if not isinstance(value, dict):
            raise ValueError("state must be an object")
        return value

    def _integrity_snapshot_locked(
        self, workspace: Path
    ) -> WorkspaceSnapshot | None:
        if self.integrity_resources is None:
            return None
        return validate_workspace_integrity(workspace, self.integrity_resources)

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
        transaction_id: str,
        workspace_identity: tuple[int, int],
        workspace: Path,
        previous: dict[Path, bytes | None],
        created_directories: list[Path],
        staged: list[_StagedFile],
    ) -> dict[str, object]:
        return {
            "schema_version": 3,
            "transaction_id": transaction_id,
            "workspace_identity": {
                "device": workspace_identity[0],
                "inode": workspace_identity[1],
            },
            "journal_stage_name": self._journal_stage_name(transaction_id),
            "stage_files": [
                {
                    "name": stage.name,
                    "device": stage.identity[0],
                    "inode": stage.identity[1],
                }
                for stage in staged
            ],
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

    def _recover_locked(self, workspace: Path, workspace_fd: int | None = None) -> None:
        owns_workspace_fd = workspace_fd is None
        if workspace_fd is None:
            workspace_fd = self._open_workspace_directory(workspace)
        try:
            try:
                journal_status = os.stat(
                    _TRANSACTION_JOURNAL,
                    dir_fd=workspace_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                self._reject_unowned_stage_entries(workspace_fd)
                if owns_workspace_fd:
                    os.close(workspace_fd)
                return
            if not stat.S_ISREG(journal_status.st_mode):
                raise ValueError("invalid transaction journal")
            value = self._read_json_entry(
                workspace_fd,
                _TRANSACTION_JOURNAL,
                expected_identity=self._identity(journal_status),
            )
            transaction_id = value.get("transaction_id") if isinstance(value, dict) else None
            targets = value.get("targets") if isinstance(value, dict) else None
            created = value.get("created_directories", []) if isinstance(value, dict) else None
            stages = value.get("stage_files") if isinstance(value, dict) else None
            journal_stage = value.get("journal_stage_name") if isinstance(value, dict) else None
            workspace_identity = self._journal_identity(value, "workspace_identity")
            if (
                value.get("schema_version") != 3
                or not isinstance(transaction_id, str)
                or _TRANSACTION_ID.fullmatch(transaction_id) is None
                or not isinstance(targets, list)
                or not isinstance(created, list)
                or not isinstance(stages, list)
                or journal_stage != self._journal_stage_name(transaction_id)
                or workspace_identity != self._descriptor_identity(workspace_fd)
            ):
                raise ValueError("invalid transaction journal")
            stage_identities = self._journal_stage_identities(
                transaction_id, stages, len(targets)
            )
            allowed_stage_names = set(stage_identities) | {journal_stage}
            present_stage_names = {
                name
                for name in os.listdir(workspace_fd)
                if name.startswith(_TRANSACTION_STAGE_PREFIX)
            }
            if not present_stage_names.issubset(allowed_stage_names):
                raise ValueError("invalid transaction journal")
            for name in present_stage_names:
                entry = os.stat(name, dir_fd=workspace_fd, follow_symlinks=False)
                if not stat.S_ISREG(entry.st_mode):
                    raise ValueError("invalid transaction journal")
                identity = self._identity(entry)
                if name == journal_stage:
                    if identity != self._identity(journal_status):
                        raise ValueError("invalid transaction journal")
                elif identity != stage_identities[name]:
                    raise ValueError("invalid transaction journal")
            snapshots: list[tuple[str, bytes | None]] = []
            for entry in targets:
                if not isinstance(entry, dict):
                    raise ValueError("invalid transaction journal")
                relative = entry.get("relative")
                before = entry.get("before")
                if not isinstance(relative, str) or before is not None and not isinstance(before, str):
                    raise ValueError("invalid transaction journal")
                target = self._transaction_target(workspace, relative)
                body = None if before is None else base64.b64decode(before.encode("ascii"), validate=True)
                snapshots.append((str(target.relative_to(workspace)), body))
            created_directories = [
                str(self._transaction_directory(workspace, relative).relative_to(workspace))
                for relative in created
                if isinstance(relative, str)
            ]
            if len(created_directories) != len(created):
                raise ValueError("invalid transaction journal")
        except (OSError, ValueError, TypeError) as exc:
            if owns_workspace_fd:
                os.close(workspace_fd)
            if isinstance(exc, ValueError) and str(exc) == "unexpected transaction stage":
                raise
            raise ValueError("invalid transaction journal") from exc
        try:
            for relative, body in snapshots:
                self._restore_target_bytes(workspace_fd, relative, body)
            self._cleanup_created_directories(workspace_fd, created_directories)
            self._cleanup_journal_stages(workspace_fd, stage_identities)
            if self._entry_exists(workspace_fd, journal_stage):
                self._unlink_matching_entry(
                    workspace_fd, journal_stage, self._identity(journal_status)
                )
            self._unlink_matching_entry(
                workspace_fd, _TRANSACTION_JOURNAL, self._identity(journal_status)
            )
        finally:
            if owns_workspace_fd:
                os.close(workspace_fd)

    @staticmethod
    def _journal_identity(value: object, key: str) -> tuple[int, int]:
        identity = value.get(key) if isinstance(value, dict) else None
        device = identity.get("device") if isinstance(identity, dict) else None
        inode = identity.get("inode") if isinstance(identity, dict) else None
        if not isinstance(device, int) or not isinstance(inode, int):
            raise ValueError("invalid transaction journal")
        return device, inode

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
    def _entry_exists(parent_fd: int, name: str) -> bool:
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    @staticmethod
    def _stage_name(transaction_id: str, index: int) -> str:
        return f"{_TRANSACTION_STAGE_PREFIX}{transaction_id}-{index}.stage"

    @staticmethod
    def _journal_stage_name(transaction_id: str) -> str:
        return f"{_TRANSACTION_STAGE_PREFIX}{transaction_id}-journal.stage"

    def _reject_unowned_stage_entries(self, workspace_fd: int) -> None:
        if any(name.startswith(_TRANSACTION_STAGE_PREFIX) for name in os.listdir(workspace_fd)):
            raise ValueError("unexpected transaction stage")

    def _journal_stage_identities(
        self, transaction_id: str, entries: list[object], target_count: int
    ) -> dict[str, tuple[int, int]]:
        if len(entries) != target_count:
            raise ValueError("invalid transaction journal")
        identities: dict[str, tuple[int, int]] = {}
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ValueError("invalid transaction journal")
            name = entry.get("name")
            device = entry.get("device")
            inode = entry.get("inode")
            if (
                name != self._stage_name(transaction_id, index)
                or not isinstance(device, int)
                or not isinstance(inode, int)
            ):
                raise ValueError("invalid transaction journal")
            identities[name] = (device, inode)
        return identities

    def _cleanup_journal_stages(
        self, workspace_fd: int, identities: dict[str, tuple[int, int]]
    ) -> None:
        for name, identity in identities.items():
            if self._entry_exists(workspace_fd, name):
                self._unlink_matching_entry(workspace_fd, name, identity)

    def _cleanup_staged_files(
        self, workspace_fd: int, stages: list[_StagedFile]
    ) -> None:
        for stage in stages:
            if self._entry_exists(workspace_fd, stage.name):
                self._unlink_matching_entry(workspace_fd, stage.name, stage.identity)

    def _unlink_matching_entry(
        self, workspace_fd: int, name: str, identity: tuple[int, int]
    ) -> None:
        entry = os.stat(name, dir_fd=workspace_fd, follow_symlinks=False)
        if not stat.S_ISREG(entry.st_mode) or self._identity(entry) != identity:
            raise OSError(f"transaction staging cleanup incomplete: {name}")
        self._unlink_stage_entry(workspace_fd, name)
        if self._entry_exists(workspace_fd, name):
            raise OSError(f"transaction staging cleanup incomplete: {name}")

    @staticmethod
    def _unlink_stage_entry(workspace_fd: int, name: str) -> None:
        os.unlink(name, dir_fd=workspace_fd)

    def _write_transaction_journal(
        self,
        workspace_fd: int,
        transaction_id: str,
        value: dict[str, object],
        *,
        mark_journal_created: Callable[[], None] | None = None,
    ) -> tuple[int, int]:
        temporary = self._stage_bytes(
            workspace_fd,
            self._journal_stage_name(transaction_id),
            self._json_bytes(value),
        )
        journal_created = False
        try:
            if self._entry_exists(workspace_fd, _TRANSACTION_JOURNAL):
                raise ValueError("invalid transaction journal")
            self._assert_stage_entry(workspace_fd, temporary)
            try:
                os.link(
                    temporary.name,
                    _TRANSACTION_JOURNAL,
                    src_dir_fd=workspace_fd,
                    dst_dir_fd=workspace_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise ValueError("invalid transaction journal") from exc
            journal_created = True
            if mark_journal_created is not None:
                mark_journal_created()
            journal = os.stat(
                _TRANSACTION_JOURNAL,
                dir_fd=workspace_fd,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(journal.st_mode) or self._identity(journal) != temporary.identity:
                raise ValueError("invalid transaction journal")
            self._unlink_matching_entry(
                workspace_fd, temporary.name, temporary.identity
            )
            os.fsync(workspace_fd)
            return temporary.identity
        except BaseException:
            if (
                not journal_created
                and self._entry_exists(workspace_fd, temporary.name)
            ):
                self._unlink_matching_entry(
                    workspace_fd, temporary.name, temporary.identity
                )
            raise
        finally:
            temporary.close()

    @staticmethod
    def _read_json_entry(
        workspace_fd: int,
        name: str,
        *,
        expected_identity: tuple[int, int] | None = None,
    ) -> object:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=workspace_fd)
        try:
            status = os.fstat(descriptor)
            if (
                not stat.S_ISREG(status.st_mode)
                or expected_identity is not None
                and WorkspaceStore._identity(status) != expected_identity
            ):
                raise ValueError("invalid transaction journal")
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                return json.load(handle)
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _transaction_target(self, workspace: Path, relative: str) -> Path:
        candidate = Path(relative)
        if (
            not relative
            or candidate.is_absolute()
            or any(part in {"", ".", ".."} for part in candidate.parts)
        ):
            raise ValueError("invalid transaction journal")
        if any(
            part in {_TRANSACTION_JOURNAL, ".transactions"}
            or part.startswith(_TRANSACTION_STAGE_PREFIX)
            for part in candidate.parts
        ):
            raise ValueError("invalid transaction journal")
        return workspace.joinpath(*candidate.parts)

    def _transaction_directory(self, workspace: Path, relative: str) -> Path:
        directory = self._transaction_target(workspace, relative)
        if directory == workspace or directory.name in {
            ".transaction.json", "goal.yaml", "plan.yaml", "state.json", "events.jsonl"
        }:
            raise ValueError("invalid transaction journal")
        return directory

    def _cleanup_created_directories(
        self, workspace_fd: int, created_directories: list[str]
    ) -> None:
        for relative in reversed(created_directories):
            parent_fd, name = self._open_target_parent(
                workspace_fd, relative, create=False
            )
            if parent_fd is None:
                continue
            try:
                try:
                    entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISDIR(entry.st_mode):
                    raise OSError(
                        f"transaction-created directory cleanup incomplete: {relative}"
                    )
                self._remove_created_directory(parent_fd, name)
                if self._entry_exists(parent_fd, name):
                    raise OSError(
                        f"transaction-created directory cleanup incomplete: {relative}"
                    )
            finally:
                os.close(parent_fd)

    @staticmethod
    def _remove_created_directory(parent_fd: int, name: str) -> None:
        os.rmdir(name, dir_fd=parent_fd)

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
        if any(
            part in {_TRANSACTION_JOURNAL, ".transactions"}
            or part.startswith(_TRANSACTION_STAGE_PREFIX)
            for part in candidate.parts
        ):
            raise ValueError("artifact path is reserved")
        target = (workspace / candidate).resolve()
        try:
            rendered = target.relative_to(workspace)
        except ValueError as exc:
            raise ValueError("artifact path escapes workspace") from exc
        if target == workspace or target.name in {
            "meta.json", "goal.yaml", "plan.yaml", "state.json", "events.jsonl"
        }:
            raise ValueError("artifact path is reserved")
        if rendered.parts[:1] not in {("artifacts",), ("runs",)}:
            raise ValueError("artifact path must start with artifacts/ or runs/")
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

    def _stage_bytes(self, workspace_fd: int, name: str, body: bytes) -> _StagedFile:
        if (
            _TRANSACTION_STAGE.fullmatch(name) is None
            and not re.fullmatch(r"\.twin-txn-[0-9a-f]{32}-journal\.stage", name)
        ):
            raise ValueError("invalid transaction stage name")
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=workspace_fd,
        )
        try:
            remaining = memoryview(body)
            while remaining:
                written = os.write(descriptor, remaining)
                remaining = remaining[written:]
            os.fsync(descriptor)
            stage = _StagedFile(
                name=name,
                descriptor=descriptor,
                identity=self._descriptor_identity(descriptor),
            )
            self._assert_stage_entry(workspace_fd, stage)
            return stage
        except BaseException:
            try:
                entry = os.stat(name, dir_fd=workspace_fd, follow_symlinks=False)
                opened = os.fstat(descriptor)
                if stat.S_ISREG(entry.st_mode) and self._identity(entry) == self._identity(opened):
                    os.unlink(name, dir_fd=workspace_fd)
            except (FileNotFoundError, OSError):
                pass
            os.close(descriptor)
            raise

    def _assert_stage_entry(self, workspace_fd: int, stage: _StagedFile) -> None:
        current = os.stat(stage.name, dir_fd=workspace_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(current.st_mode)
            or self._identity(current) != stage.identity
            or self._descriptor_identity(stage.descriptor) != stage.identity
        ):
            raise ValueError("transaction stage was replaced")

    def _publish_staged(
        self,
        staged: dict[Path, _StagedFile],
        previous: dict[Path, bytes | None],
        state_path: Path,
        workspace_fd: int,
    ) -> None:
        ordered = [target for target in staged if target != state_path] + [state_path]
        for target in ordered:
            stage = staged[target]
            self._assert_stage_entry(workspace_fd, stage)
            relative = str(target.relative_to(state_path.parent))
            parent_fd, name = self._open_target_parent(
                workspace_fd, relative, create=True
            )
            assert parent_fd is not None
            try:
                os.replace(
                    stage.name,
                    name,
                    src_dir_fd=workspace_fd,
                    dst_dir_fd=parent_fd,
                )
            finally:
                os.close(parent_fd)

    def _read_target_bytes(self, workspace_fd: int, relative: str) -> bytes | None:
        parent_fd, name = self._open_target_parent(workspace_fd, relative, create=False)
        if parent_fd is None:
            return None
        try:
            try:
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            except FileNotFoundError:
                return None
            try:
                status = os.fstat(descriptor)
                if not stat.S_ISREG(status.st_mode):
                    raise ValueError("unsafe transaction target")
                with os.fdopen(descriptor, "rb") as handle:
                    descriptor = -1
                    return handle.read()
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        finally:
            os.close(parent_fd)

    def _restore_target_bytes(
        self, workspace_fd: int, relative: str, body: bytes | None
    ) -> None:
        parent_fd, name = self._open_target_parent(
            workspace_fd, relative, create=body is not None
        )
        if parent_fd is None:
            return
        try:
            if body is None:
                try:
                    entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return
                if not stat.S_ISREG(entry.st_mode):
                    raise ValueError("unsafe transaction target")
                os.unlink(name, dir_fd=parent_fd)
                return
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            try:
                remaining = memoryview(body)
                while remaining:
                    written = os.write(descriptor, remaining)
                    remaining = remaining[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            os.close(parent_fd)

    def _open_target_parent(
        self, workspace_fd: int, relative: str, *, create: bool
    ) -> tuple[int | None, str]:
        candidate = Path(relative)
        if (
            not relative
            or candidate.is_absolute()
            or any(part in {"", ".", ".."} for part in candidate.parts)
        ):
            raise ValueError("unsafe transaction target")
        current = os.dup(workspace_fd)
        try:
            for part in candidate.parts[:-1]:
                try:
                    child = self._open_directory_entry(current, part)
                except FileNotFoundError:
                    if not create:
                        os.close(current)
                        return None, candidate.name
                    os.mkdir(part, 0o700, dir_fd=current)
                    child = self._open_directory_entry(current, part)
                os.close(current)
                current = child
            return current, candidate.name
        except BaseException:
            os.close(current)
            raise
