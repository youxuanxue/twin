import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from twin.paths import TwinPaths
from twin.resources import ResourceCatalog
from twin.schema import validate_document
from twin.storage.workspaces import WorkspaceStore
from twin.yaml_codec import load_yaml


class WorkspaceStoreTest(TestCase):
    def test_create_writes_outside_target_repo(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            store = WorkspaceStore(TwinPaths.for_home(root / "home"))
            workspace_id = store.create("ship feature", repo, "host/codex")
            workspace = store.resolve(workspace_id, repo)
            self.assertTrue(str(workspace).startswith(str(root / "home" / ".twin")))
            self.assertFalse((repo / ".twin").exists())

    def test_revision_mismatch_fails_closed(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            store = WorkspaceStore(TwinPaths.for_home(root / "home"))
            workspace = store.resolve(store.create("ship feature", repo, "host/codex"), repo)
            state = store.load_state(workspace)
            with self.assertRaisesRegex(ValueError, "state revision mismatch"):
                store.replace_state(workspace, 99, state)

    def test_create_writes_schema_valid_drafts_and_initial_state(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            store = WorkspaceStore(TwinPaths.for_home(root / "home"))
            workspace = store.resolve(store.create("ship feature", repo, "host/codex"), repo)
            resources = ResourceCatalog(Path(__file__).resolve().parents[1])
            self.assertEqual(validate_document(load_yaml(workspace / "goal.yaml"), "goal", resources), [])
            self.assertEqual(validate_document(load_yaml(workspace / "plan.yaml"), "plan", resources), [])
            self.assertEqual(validate_document(store.load_state(workspace), "state", resources), [])

    def test_event_details_redact_answer_instruction_output_tokens_and_secrets(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            store = WorkspaceStore(TwinPaths.for_home(root / "home"))
            workspace = store.resolve(store.create("ship feature", repo, "host/codex"), repo)
            store.append_event(workspace, {
                "event": "audit",
                "details": {
                    "answer": "human answer",
                    "host_instruction": "do not retain this",
                    "provider_output": "do not retain this",
                    "access_token": "do not retain this",
                    "secret_key": "do not retain this",
                    "artifact": "artifacts/result.json",
                    "bytes": 3,
                },
            })
            event = json.loads((workspace / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(event["details"], {"artifact": "artifacts/result.json", "bytes": 3})

    def test_artifact_cannot_overwrite_revision_bound_state(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            store = WorkspaceStore(TwinPaths.for_home(root / "home"))
            workspace = store.resolve(store.create("ship feature", repo, "host/codex"), repo)
            with self.assertRaisesRegex(ValueError, "artifact path is reserved"):
                store.write_artifact(workspace, "state.json", b"not state")
