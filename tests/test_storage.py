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

    def test_state_replacement_records_event_at_next_revision(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            store = WorkspaceStore(TwinPaths.for_home(root / "home"))
            workspace = store.resolve(store.create("ship feature", repo, "host/codex"), repo)
            state = store.load_state(workspace)
            store.replace_state(workspace, 0, state)
            replaced = store.load_state(workspace)
            event = json.loads((workspace / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(replaced["state_revision"], 1)
            self.assertEqual(event, {
                "schema_version": 1,
                "event": "state_replaced",
                "workspace_id": replaced["workspace_id"],
                "state_revision": 1,
                "details": {},
                "recorded_at": event["recorded_at"],
            })

    def test_action_context_rejects_unknown_fields(self) -> None:
        action = {
            "contract_version": 1,
            "action": "author_plan",
            "workspace": "workspace",
            "supervisor_route": "host/codex",
            "state_revision": 1,
            "action_token": "token",
            "context": {"unknown": "field"},
            "expected_output": {},
            "submit": {},
        }
        resources = ResourceCatalog(Path(__file__).resolve().parents[1])
        self.assertIn("$.context: unexpected property 'unknown'", validate_document(action, "action", resources))

    def test_action_expected_output_rejects_unknown_fields(self) -> None:
        action = {
            "contract_version": 1,
            "action": "author_plan",
            "workspace": "workspace",
            "supervisor_route": "host/codex",
            "state_revision": 1,
            "action_token": "token",
            "context": {},
            "expected_output": {"unknown": "field"},
            "submit": {},
        }
        resources = ResourceCatalog(Path(__file__).resolve().parents[1])
        self.assertIn("$.expected_output: unexpected property 'unknown'", validate_document(action, "action", resources))

    def test_run_request_rejects_unknown_fields(self) -> None:
        evidence = {
            "schema_version": 1,
            "run_id": "run-1",
            "item_id": "item-1",
            "request": {"unknown": "field"},
            "result": {},
            "evidence": [],
            "status": "completed",
        }
        resources = ResourceCatalog(Path(__file__).resolve().parents[1])
        self.assertIn("$.request: unexpected property 'unknown'", validate_document(evidence, "run-evidence", resources))

    def test_run_result_rejects_unknown_fields(self) -> None:
        evidence = {
            "schema_version": 1,
            "run_id": "run-1",
            "item_id": "item-1",
            "request": {},
            "result": {"unknown": "field"},
            "evidence": [],
            "status": "completed",
        }
        resources = ResourceCatalog(Path(__file__).resolve().parents[1])
        self.assertIn("$.result: unexpected property 'unknown'", validate_document(evidence, "run-evidence", resources))

    def test_load_yaml_continues_multiline_single_quoted_scalar(self) -> None:
        with TemporaryDirectory() as raw:
            path = Path(raw) / "value.yaml"
            path.write_text("title: 'first line\n  second line'\nnext: value\n", encoding="utf-8")
            self.assertEqual(load_yaml(path), {
                "title": "first line\nsecond line",
                "next": "value",
            })
