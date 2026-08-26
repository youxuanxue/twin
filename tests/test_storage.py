import json
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from twin.domain.service import TwinService
from twin.paths import TwinPaths
from twin.resources import ResourceCatalog
from twin.schema import validate_document
from twin.storage.workspaces import WorkspaceStore
from twin.yaml_codec import encode_yaml, load_yaml


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

    def test_artifact_paths_must_use_an_audited_root(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            store = WorkspaceStore(TwinPaths.for_home(root / "home"))
            workspace = store.resolve(store.create("ship feature", repo, "host/codex"), repo)

            with self.assertRaisesRegex(ValueError, "artifact path must start with"):
                store.write_artifact(workspace, "sidecar.txt", b"not audited")

            self.assertFalse((workspace / "sidecar.txt").exists())

    def test_commit_rejects_duplicate_canonical_artifact_targets(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            store = WorkspaceStore(TwinPaths.for_home(root / "home"))
            workspace = store.resolve(store.create("ship feature", repo, "host/codex"), repo)
            state = store.load_state(workspace)

            with self.assertRaisesRegex(ValueError, "duplicate artifact path"):
                store.commit_action(
                    workspace,
                    int(state["state_revision"]),
                    state,
                    documents={},
                    artifacts={
                        "artifacts/result.txt": b"first",
                        "artifacts/./result.txt": b"second",
                    },
                    event=None,
                    validate_current=lambda current: None,
                )

            self.assertFalse((workspace / "artifacts" / "result.txt").exists())
            self.assertEqual(store.load_state(workspace)["state_revision"], 0)

    def test_artifact_write_is_not_redirected_by_a_parent_symlink_swap(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            outside = root / "outside"
            outside.mkdir()
            store = WorkspaceStore(TwinPaths.for_home(root / "home"))
            workspace = store.resolve(store.create("ship feature", repo, "host/codex"), repo)

            original_artifact_path = store._artifact_path

            def swap_parent(candidate_workspace: Path, relative: str) -> Path:
                target = original_artifact_path(candidate_workspace, relative)
                artifact_root = workspace / "artifacts"
                artifact_root.rename(workspace / "artifacts-original")
                artifact_root.symlink_to(outside, target_is_directory=True)
                return target

            with patch.object(store, "_artifact_path", side_effect=swap_parent):
                with self.assertRaises((OSError, ValueError)):
                    store.write_artifact(
                        workspace, "artifacts/race.txt", b"descriptor anchored"
                    )

            self.assertFalse((outside / "race.txt").exists())

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

    def test_status_rejects_malformed_state_deleted_revision_and_untracked_artifact(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            resources = ResourceCatalog(Path(__file__).resolve().parents[1])

            for corruption in ("state", "events", "artifact"):
                with self.subTest(corruption=corruption):
                    store = WorkspaceStore(TwinPaths.for_home(root / f"home-{corruption}"))
                    service = TwinService(store, resources=resources)
                    action = service.start("ship feature", repo, "host/codex")
                    workspace = store.resolve(str(action["workspace"]), repo)
                    if corruption == "state":
                        state = json.loads((workspace / "state.json").read_text(encoding="utf-8"))
                        state["unknown"] = True
                        (workspace / "state.json").write_text(json.dumps(state), encoding="utf-8")
                        expected = "invalid state"
                    elif corruption == "events":
                        lines = (workspace / "events.jsonl").read_text(encoding="utf-8").splitlines()
                        (workspace / "events.jsonl").write_text(lines[0] + "\n", encoding="utf-8")
                        expected = "event revision"
                    else:
                        extra = workspace / "artifacts" / "untracked.txt"
                        extra.write_text("not audited", encoding="utf-8")
                        expected = "untracked artifact"
                    with self.assertRaisesRegex(ValueError, expected):
                        service.status(action["workspace"], repo)

    def test_status_rejects_pending_action_and_repository_identity_inconsistency(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            resources = ResourceCatalog(Path(__file__).resolve().parents[1])
            for corruption in ("pending", "repository"):
                with self.subTest(corruption=corruption):
                    store = WorkspaceStore(TwinPaths.for_home(root / f"home-{corruption}"))
                    service = TwinService(store, resources=resources)
                    action = service.start("ship feature", repo, "host/codex")
                    workspace = store.resolve(str(action["workspace"]), repo)
                    state = json.loads((workspace / "state.json").read_text(encoding="utf-8"))
                    if corruption == "pending":
                        state["pending_action"] = None
                        expected = "pending action invariant"
                    else:
                        state["repository_identity"] = "0" * 64
                        expected = "repository identity mismatch"
                    (workspace / "state.json").write_text(json.dumps(state), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, expected):
                        service.status(action["workspace"], repo)

    def test_status_rejects_incomplete_pending_action(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            resources = ResourceCatalog(Path(__file__).resolve().parents[1])
            required_fields = (
                "kind", "state_revision", "route", "token_hash", "run_id",
                "repository_identity",
            )

            for field in required_fields:
                with self.subTest(field=field):
                    store = WorkspaceStore(TwinPaths.for_home(root / f"home-{field}"))
                    service = TwinService(store, resources=resources)
                    action = service.start("ship feature", repo, "host/codex")
                    workspace = store.resolve(str(action["workspace"]), repo)
                    state = json.loads((workspace / "state.json").read_text(encoding="utf-8"))
                    pending = state["pending_action"]
                    assert isinstance(pending, dict)
                    del pending[field]
                    (workspace / "state.json").write_text(json.dumps(state), encoding="utf-8")

                    with self.assertRaises(ValueError):
                        service.status(action["workspace"], repo)

    def test_status_rejects_invalid_pending_action_kind_and_token_hash(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            resources = ResourceCatalog(Path(__file__).resolve().parents[1])
            cases = (
                ("kind", "worker"),
                ("token_hash", "not-a-sha256"),
            )

            for field, value in cases:
                with self.subTest(field=field):
                    store = WorkspaceStore(TwinPaths.for_home(root / f"home-{field}"))
                    service = TwinService(store, resources=resources)
                    action = service.start("ship feature", repo, "host/codex")
                    workspace = store.resolve(str(action["workspace"]), repo)
                    state = json.loads((workspace / "state.json").read_text(encoding="utf-8"))
                    pending = state["pending_action"]
                    assert isinstance(pending, dict)
                    pending[field] = value
                    (workspace / "state.json").write_text(json.dumps(state), encoding="utf-8")

                    with self.assertRaises(ValueError):
                        service.status(action["workspace"], repo)

    def test_status_rejects_symlinked_artifact_roots(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            resources = ResourceCatalog(Path(__file__).resolve().parents[1])

            for root_name in ("artifacts", "runs"):
                with self.subTest(root_name=root_name):
                    store = WorkspaceStore(TwinPaths.for_home(root / f"home-{root_name}"))
                    service = TwinService(store, resources=resources)
                    action = service.start("ship feature", repo, "host/codex")
                    workspace = store.resolve(str(action["workspace"]), repo)
                    artifact_root = workspace / root_name
                    if artifact_root.exists():
                        artifact_root.rmdir()
                    external = root / f"external-{root_name}"
                    external.mkdir()
                    artifact_root.symlink_to(external, target_is_directory=True)

                    with self.assertRaisesRegex(ValueError, "artifact symlink"):
                        service.status(action["workspace"], repo)

    def test_status_rejects_run_evidence_cross_reference_drift(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            resources = ResourceCatalog(Path(__file__).resolve().parents[1])
            store = WorkspaceStore(TwinPaths.for_home(root / "home"))
            service = TwinService(
                store,
                runtime=_IntegrityRuntime(),
                resources=resources,
            )
            action = service.start("ship feature", repo, "host/codex")
            ready = service.submit_plan(
                action["workspace"], "host/codex", action["state_revision"],
                action["action_token"], _integrity_goal_and_plan(),
            )
            review = service.run(ready["workspace"], repo, "host/codex")
            workspace = store.resolve(str(review["workspace"]), repo)
            run_id = review["context"]["run"]["run_id"]
            evidence_path = workspace / "runs" / run_id / "evidence.json"
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["request"]["sha256"] = "0" * 64
            body = (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode("utf-8")
            evidence_path.write_bytes(body)
            events = [
                json.loads(line)
                for line in (workspace / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            for event in events:
                if (
                    event.get("event") == "artifact_written"
                    and event.get("details", {}).get("relative") == f"runs/{run_id}/evidence.json"
                ):
                    event["details"]["sha256"] = hashlib.sha256(body).hexdigest()
                    event["details"]["bytes"] = len(body)
            (workspace / "events.jsonl").write_text(
                "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "run evidence reference mismatch"):
                service.status(review["workspace"], repo)

    def test_status_rejects_run_evidence_status_that_contradicts_result(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            resources = ResourceCatalog(Path(__file__).resolve().parents[1])
            store = WorkspaceStore(TwinPaths.for_home(root / "home"))
            service = TwinService(store, runtime=_IntegrityRuntime(), resources=resources)
            action = service.start("ship feature", repo, "host/codex")
            ready = service.submit_plan(
                action["workspace"], "host/codex", action["state_revision"],
                action["action_token"], _integrity_goal_and_plan(),
            )
            review = service.run(ready["workspace"], repo, "host/codex")
            workspace = store.resolve(str(review["workspace"]), repo)
            run_id = str(review["context"]["run"]["run_id"])
            result_relative = f"runs/{run_id}/result.json"
            evidence_relative = f"runs/{run_id}/evidence.json"
            result = json.loads((workspace / result_relative).read_text(encoding="utf-8"))
            result["returncode"] = 1
            result_metadata = _rewrite_audited_json(workspace, result_relative, result)
            evidence = json.loads((workspace / evidence_relative).read_text(encoding="utf-8"))
            evidence["result"] = result_metadata
            _rewrite_audited_json(workspace, evidence_relative, evidence)

            with self.assertRaisesRegex(ValueError, "run evidence status mismatch"):
                service.status(review["workspace"], repo)

    def test_status_rejects_run_identity_relation_drift_with_rehashed_artifacts(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            other_repo = root / "other-repo"
            other_repo.mkdir()
            resources = ResourceCatalog(Path(__file__).resolve().parents[1])
            cases = (
                ("request_workspace", "run request workspace mismatch"),
                ("request_repository", "run request repository mismatch"),
                ("request_item", "run request item mismatch"),
                ("evidence_item", "run evidence item mismatch"),
                ("state_item", "worker run item mismatch"),
            )
            for corruption, expected in cases:
                with self.subTest(corruption=corruption):
                    store = WorkspaceStore(TwinPaths.for_home(root / f"home-{corruption}"))
                    service = TwinService(
                        store,
                        runtime=_IntegrityRuntime(),
                        resources=resources,
                    )
                    action = service.start("ship feature", repo, "host/codex")
                    ready = service.submit_plan(
                        action["workspace"], "host/codex", action["state_revision"],
                        action["action_token"], _integrity_goal_and_plan(),
                    )
                    review = service.run(ready["workspace"], repo, "host/codex")
                    workspace = store.resolve(str(review["workspace"]), repo)
                    run_id = str(review["context"]["run"]["run_id"])
                    if corruption == "state_item":
                        state_path = workspace / "state.json"
                        state = json.loads(state_path.read_text(encoding="utf-8"))
                        state["current_item_id"] = "other-item"
                        state_path.write_text(json.dumps(state), encoding="utf-8")
                    elif corruption == "evidence_item":
                        evidence_relative = f"runs/{run_id}/evidence.json"
                        evidence = json.loads((workspace / evidence_relative).read_text(encoding="utf-8"))
                        evidence["item_id"] = "other-item"
                        _rewrite_audited_json(workspace, evidence_relative, evidence)
                    else:
                        request_relative = f"runs/{run_id}/request.json"
                        request = json.loads((workspace / request_relative).read_text(encoding="utf-8"))
                        if corruption == "request_workspace":
                            request["workspace_id"] = "other-workspace"
                        elif corruption == "request_repository":
                            request["repository_root"] = str(other_repo.resolve())
                        else:
                            request["item_id"] = "other-item"
                        request_metadata = _rewrite_audited_json(
                            workspace, request_relative, request
                        )
                        evidence_relative = f"runs/{run_id}/evidence.json"
                        evidence = json.loads((workspace / evidence_relative).read_text(encoding="utf-8"))
                        evidence["request"] = request_metadata
                        _rewrite_audited_json(workspace, evidence_relative, evidence)

                    with self.assertRaisesRegex(ValueError, expected):
                        service.status(review["workspace"], repo)

    def test_status_rejects_incomplete_plan_in_accepted_terminal_state(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            resources = ResourceCatalog(Path(__file__).resolve().parents[1])
            store = WorkspaceStore(TwinPaths.for_home(root / "home"))
            service = TwinService(store, runtime=_IntegrityRuntime(), resources=resources)
            action = service.start("ship feature", repo, "host/codex")
            ready = service.submit_plan(
                action["workspace"], "host/codex", action["state_revision"],
                action["action_token"], _integrity_goal_and_plan(),
            )
            review = service.run(ready["workspace"], repo, "host/codex")
            accepted = service.submit_review(
                review["workspace"], "host/codex", review["state_revision"],
                review["action_token"], review["context"]["run"]["run_id"],
                {"decision": "accepted"},
            )
            workspace = store.resolve(str(accepted["workspace"]), repo)
            plan = load_yaml(workspace / "plan.yaml")
            item = plan["items"][0]
            assert isinstance(item, dict)
            item["status"] = "pending"
            item["actual_evidence"] = []
            (workspace / "plan.yaml").write_bytes(encode_yaml(plan))

            with self.assertRaisesRegex(ValueError, "accepted completion invariant"):
                service.status(accepted["workspace"], repo)

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

    def test_yaml_round_trip_preserves_ambiguous_strings_as_strings(self) -> None:
        values = (
            "true", "false", "null", "None", "~", "123", "1.0", "[]", "{}",
            "[one,two]", " leading", "trailing ", "a:b", "#hash", "O'Brien", "",
            "Unicode 雪蕉", "plain",
        )
        with TemporaryDirectory() as raw:
            path = Path(raw) / "values.yaml"
            for value in values:
                with self.subTest(value=value):
                    path.write_bytes(encode_yaml({"value": value}))
                    self.assertEqual(load_yaml(path), {"value": value})

    def test_draft_schema_rejects_malformed_nonempty_goal_and_plan_entries(self) -> None:
        resources = ResourceCatalog(Path(__file__).resolve().parents[1])
        goal = {
            "schema_version": 1,
            "id": "workspace",
            "one_liner": "Ship",
            "core_goal": "Ship safely",
            "acceptance_criteria": ["not-an-object"],
            "non_goals": [1],
        }
        plan = {
            "schema_version": 1,
            "goal_id": "workspace",
            "items": [{"id": "incomplete"}],
            "verification": [1],
        }

        goal_errors = validate_document(goal, "goal", resources)
        plan_errors = validate_document(plan, "plan", resources)

        self.assertIn("$.acceptance_criteria[0]: expected object, got str", goal_errors)
        self.assertIn("$.non_goals[0]: expected string, got int", goal_errors)
        self.assertIn("$.items[0]: missing required 'deliverable'", plan_errors)
        self.assertIn("$.verification[0]: expected string, got int", plan_errors)


def _integrity_goal_and_plan() -> dict[str, object]:
    return {
        "goal": {
            "schema_version": 1,
            "id": "assigned",
            "one_liner": "Verify integrity",
            "core_goal": "Verify integrity",
            "acceptance_criteria": [{
                "id": "ac-1", "statement": "Evidence is recorded", "evidence_type": "artifact",
            }],
            "non_goals": [],
        },
        "plan": {
            "schema_version": 1,
            "goal_id": "assigned",
            "items": [{
                "id": "verify", "deliverable": "Evidence", "scope": "Test only",
                "covers_ac": ["ac-1"], "evidence_plan": ["artifacts/evidence.txt"],
                "actual_evidence": [], "depends_on": [], "status": "pending",
                "next_action": "Record evidence",
            }],
            "verification": ["run tests"],
        },
    }


class _IntegrityRuntime:
    def run_turn(self, request: object) -> object:
        del request
        from twin.runtime.protocols import WorkerTurnResult

        return WorkerTurnResult(
            output_text="done",
            returncode=0,
            session_id="integrity",
            events=({"event": "completed"},),
            submission={
                "updates": [{
                    "item_id": "verify", "status": "completed",
                    "actual_evidence": ["artifacts/evidence.txt"],
                }],
                "command_results": [],
                "artifacts": [{"relative": "artifacts/evidence.txt", "content": "verified"}],
            },
        )


def _rewrite_audited_json(
    workspace: Path, relative: str, value: dict[str, object]
) -> dict[str, object]:
    body = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (workspace / relative).write_bytes(body)
    metadata: dict[str, object] = {
        "relative": relative,
        "sha256": hashlib.sha256(body).hexdigest(),
        "bytes": len(body),
    }
    events_path = workspace / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    matches = 0
    for event in events:
        details = event.get("details")
        if (
            event.get("event") == "artifact_written"
            and isinstance(details, dict)
            and details.get("relative") == relative
        ):
            details.update(metadata)
            matches += 1
    if matches != 1:
        raise AssertionError(f"expected one audit record for {relative}, found {matches}")
    events_path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )
    return metadata
