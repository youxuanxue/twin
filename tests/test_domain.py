import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import shlex
from tempfile import TemporaryDirectory
import textwrap
from unittest.mock import patch
from unittest import TestCase

from twin.domain.service import TwinService
from twin.paths import TwinPaths
from twin.resources import ResourceCatalog
from twin.runtime.protocols import WorkerTurnRequest, WorkerTurnResult
from twin.schema import validate_document
from twin.storage.workspaces import WorkspaceStore
from twin.yaml_codec import load_yaml


def valid_goal_and_plan() -> dict[str, object]:
    return {
        "goal": {
            "schema_version": 1,
            "id": "assigned-by-service",
            "one_liner": "Ship feature",
            "core_goal": "Ship feature safely",
            "acceptance_criteria": [
                {"id": "ac-1", "statement": "Feature works", "evidence_type": "artifact"},
            ],
            "non_goals": [],
        },
        "plan": {
            "schema_version": 1,
            "goal_id": "assigned-by-service",
            "items": [
                {
                    "id": "implement",
                    "deliverable": "Feature implementation",
                    "scope": "Only the requested feature",
                    "covers_ac": ["ac-1"],
                    "evidence_plan": ["artifacts/evidence.txt"],
                    "actual_evidence": [],
                    "depends_on": [],
                    "status": "pending",
                    "next_action": "Implement and verify",
                },
            ],
            "verification": ["Run focused tests"],
        },
    }


def tree_snapshot(root: Path) -> dict[str, tuple[str, bytes]]:
    snapshot: dict[str, tuple[str, bytes]] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            snapshot[relative] = ("symlink", os.readlink(path).encode("utf-8"))
        elif path.is_dir():
            snapshot[relative] = ("directory", b"")
        else:
            snapshot[relative] = ("file", path.read_bytes())
    return snapshot


def transaction_stage_name(transaction_id: str, index: int) -> str:
    return f".twin-txn-{transaction_id}-{index}.stage"


def transaction_journal_stage_name(transaction_id: str) -> str:
    return f".twin-txn-{transaction_id}-journal.stage"


class TwinServiceTest(TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        self.store = WorkspaceStore(TwinPaths.for_home(root / "home"))
        self.runtime = _DomainRuntime()
        self.service = TwinService(
            self.store,
            runtime=self.runtime,
            resources=ResourceCatalog(Path(__file__).resolve().parents[1]),
        )

    def start_and_submit_plan(self) -> dict[str, object]:
        action = self.service.start("ship feature", self.repo, "host/codex")
        return self.service.submit_plan(
            action["workspace"], "host/codex", action["state_revision"], action["action_token"], valid_goal_and_plan()
        )

    @staticmethod
    def workspace_snapshot(workspace: Path) -> dict[str, bytes]:
        return {
            relative: (workspace / relative).read_bytes()
            for relative in ("goal.yaml", "plan.yaml", "state.json", "events.jsonl")
        }

    def test_start_returns_author_plan_action(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        self.assertEqual(action["action"], "author_plan")
        self.assertEqual(action["state_revision"], 1)
        repository = action.get("repository")
        self.assertIsInstance(repository, dict)
        assert isinstance(repository, dict)
        self.assertEqual(repository["root"], str(self.repo.resolve()))
        self.assertEqual(len(repository["identity"]), 64)
        self.assertEqual(action["context"]["goal_request"], "ship feature")
        self.assertEqual(action["context"]["goal"]["id"], action["workspace"])
        self.assertEqual(action["context"]["plan"]["goal_id"], action["workspace"])
        self.assertEqual(
            action["expected_output"]["payload"]["required"], ["goal", "plan"],
        )
        expected_argv = [
            "twin", "submit-plan", "--workspace", action["workspace"], "--supervisor",
            "host/codex", "--state-revision", "1",
            f"--action-token={action['action_token']}", "--payload-file", "-", "--json",
        ]
        self.assertEqual(action["submit"]["argv"], expected_argv)
        self.assertEqual(action["submit"]["command"], shlex.join(expected_argv))
        self.assertIsNone(action["next_command"])
        self.assertEqual(validate_document(action, "action", self.service.resources), [])

    def test_workspace_results_drive_dynamic_lifecycle_continuation(self) -> None:
        author = self.service.start("ship feature", self.repo, "host/codex")
        self.assertIsNone(author["next_command"])

        planned = self.service.submit_plan(
            author["workspace"], "host/codex", author["state_revision"],
            author["action_token"], valid_goal_and_plan(),
        )
        run_argv = [
            "twin", "run", author["workspace"], "--supervisor", "host/codex", "--json",
        ]
        run_command = {"argv": run_argv, "command": shlex.join(run_argv)}
        self.assertEqual(planned["next_command"], run_command)

        review = self.service.run(planned["workspace"], self.repo, "host/codex")
        self.assertIsNone(review["next_command"])
        changes_requested = self.service.submit_review(
            review["workspace"], "host/codex", review["state_revision"],
            review["action_token"], review["context"]["run"]["run_id"],
            {"decision": "changes_requested"},
        )
        self.assertEqual(changes_requested["next_command"], run_command)

        review = self.service.run(planned["workspace"], self.repo, "host/codex")
        needs_human = self.service.submit_review(
            review["workspace"], "host/codex", review["state_revision"],
            review["action_token"], review["context"]["run"]["run_id"],
            {"decision": "needs_human"},
        )
        self.assertIsNone(needs_human["next_command"])

        resumed = self.service.respond(
            needs_human["workspace"], self.repo, "continue",
        )
        self.assertEqual(resumed["next_command"], run_command)

        self.runtime.complete_with_evidence()
        review = self.service.run(resumed["workspace"], self.repo, "host/codex")
        accepted = self.service.submit_review(
            review["workspace"], "host/codex", review["state_revision"],
            review["action_token"], review["context"]["run"]["run_id"],
            {"decision": "accepted"},
        )
        self.assertIsNone(accepted["next_command"])

    def test_action_schema_requires_executable_next_command_descriptor(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        cases = (
            ({"argv": ["twin", "run"]}, "command"),
            ({"command": "twin run"}, "argv"),
        )

        for descriptor, missing in cases:
            with self.subTest(missing=missing):
                candidate = dict(action)
                candidate["next_command"] = descriptor
                self.assertIn(
                    f"$.next_command: missing required '{missing}'",
                    validate_document(candidate, "action", self.service.resources),
                )

    def test_action_schema_requires_action_specific_context_and_output_contracts(self) -> None:
        author = self.service.start("ship feature", self.repo, "host/codex")
        ready = self.service.submit_plan(
            author["workspace"], "host/codex", author["state_revision"],
            author["action_token"], valid_goal_and_plan(),
        )
        review = self.service.run(ready["workspace"], self.repo, "host/codex")

        missing_author_context = copy.deepcopy(author)
        del missing_author_context["context"]["goal_request"]
        missing_author_output = copy.deepcopy(author)
        del missing_author_output["expected_output"]["payload"]["schema_paths"]
        missing_review_context = copy.deepcopy(review)
        del missing_review_context["context"]["run"]["evidence"]
        missing_review_output = copy.deepcopy(review)
        del missing_review_output["expected_output"]["payload"]["decision_values"]

        cases = (
            (missing_author_context, "$.context: missing required 'goal_request'"),
            (missing_author_output, "$.expected_output.payload: missing required 'schema_paths'"),
            (missing_review_context, "$.context.run: missing required 'evidence'"),
            (missing_review_output, "$.expected_output.payload: missing required 'decision_values'"),
        )
        for candidate, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                self.assertIn(
                    expected_error,
                    validate_document(candidate, "action", self.service.resources),
                )

    def test_explicit_workspace_commands_reject_a_different_repository(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        self.service.submit_plan(
            action["workspace"], "host/codex", action["state_revision"], action["action_token"],
            valid_goal_and_plan(),
        )
        other_repo = self.repo.parent / "other-repo"
        other_repo.mkdir()
        cases = (
            lambda: self.service.run(action["workspace"], other_repo, "host/codex"),
            lambda: self.service.respond(action["workspace"], other_repo, "continue"),
            lambda: self.service.status(action["workspace"], other_repo),
            lambda: self.service.handoff(
                action["workspace"], other_repo, "host/codex", "host/claude",
            ),
        )
        for operation in cases:
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(ValueError, "workspace repository mismatch"):
                    operation()

    def test_run_action_identifies_the_runnable_plan_item(self) -> None:
        self.start_and_submit_plan()
        action = self.service.run(None, self.repo, "host/codex")
        self.assertEqual(action["action"], "review")
        self.assertEqual(action["context"]["run"]["item_id"], "implement")

    def test_action_token_is_single_use(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        payload = valid_goal_and_plan()
        self.service.submit_plan(
            action["workspace"], "host/codex", action["state_revision"], action["action_token"], payload
        )
        with self.assertRaisesRegex(ValueError, "stale or consumed action"):
            self.service.submit_plan(
                action["workspace"], "host/codex", action["state_revision"], action["action_token"], payload
            )

    def test_wrong_route_cannot_submit(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        with self.assertRaisesRegex(ValueError, "supervisor route mismatch"):
            self.service.submit_plan(
                action["workspace"], "host/claude", action["state_revision"], action["action_token"], valid_goal_and_plan()
            )

    def test_submit_plan_rejects_uncovered_acceptance_criterion(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        payload = valid_goal_and_plan()
        goal = payload["goal"]
        assert isinstance(goal, dict)
        goal["acceptance_criteria"].append({"id": "ac-2", "statement": "No regression", "evidence_type": "test"})
        with self.assertRaisesRegex(ValueError, "acceptance criterion not covered by plan: ac-2"):
            self.service.submit_plan(
                action["workspace"], "host/codex", action["state_revision"], action["action_token"], payload
            )

    def test_submit_plan_rejects_malformed_acceptance_criteria(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        payload = valid_goal_and_plan()
        goal = payload["goal"]
        assert isinstance(goal, dict)
        goal["acceptance_criteria"] = ["ac-1"]

        with self.assertRaisesRegex(ValueError, r"acceptance_criteria\[0\] must be object"):
            self.service.submit_plan(
                action["workspace"], "host/codex", action["state_revision"], action["action_token"], payload
            )

    def test_submit_plan_requires_nonempty_verification_and_actionable_item_fields(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        payload = valid_goal_and_plan()
        plan = payload["plan"]
        assert isinstance(plan, dict)
        plan["verification"] = []
        item = plan["items"][0]
        assert isinstance(item, dict)
        del item["deliverable"]

        with self.assertRaisesRegex(ValueError, "verification must contain at least one command"):
            self.service.submit_plan(
                action["workspace"], "host/codex", action["state_revision"], action["action_token"], payload
            )

    def test_submit_plan_requires_at_least_one_runnable_item(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        payload = valid_goal_and_plan()
        plan = payload["plan"]
        assert isinstance(plan, dict)
        item = plan["items"][0]
        assert isinstance(item, dict)
        item["status"] = "completed"

        with self.assertRaisesRegex(ValueError, "at least one runnable item"):
            self.service.submit_plan(
                action["workspace"], "host/codex", action["state_revision"],
                action["action_token"], payload,
            )

    def test_submit_plan_rejects_duplicate_acceptance_criterion_ids(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        payload = valid_goal_and_plan()
        goal = payload["goal"]
        assert isinstance(goal, dict)
        goal["acceptance_criteria"].append({
            "id": "ac-1", "statement": "Duplicate", "evidence_type": "artifact",
        })

        with self.assertRaisesRegex(ValueError, "duplicate acceptance criterion id: ac-1"):
            self.service.submit_plan(
                action["workspace"], "host/codex", action["state_revision"], action["action_token"], payload
            )

    def test_submit_plan_round_trips_ambiguous_strings_without_type_drift(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        payload = valid_goal_and_plan()
        goal = payload["goal"]
        plan = payload["plan"]
        assert isinstance(goal, dict) and isinstance(plan, dict)
        goal["one_liner"] = "true"
        plan["items"][0]["next_action"] = "123"

        self.service.submit_plan(
            action["workspace"], "host/codex", action["state_revision"], action["action_token"], payload
        )

        workspace = self.store.resolve(str(action["workspace"]), self.repo)
        self.assertEqual(load_yaml(workspace / "goal.yaml")["one_liner"], "true")
        self.assertEqual(load_yaml(workspace / "plan.yaml")["items"][0]["next_action"], "123")

    def test_submit_plan_reports_the_committed_revision(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        result = self.service.submit_plan(
            action["workspace"], "host/codex", action["state_revision"], action["action_token"], valid_goal_and_plan()
        )
        self.assertEqual(result["state_revision"], 2)

    def test_dependent_item_cannot_complete_before_its_dependency(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        payload = valid_goal_and_plan()
        plan = payload["plan"]
        assert isinstance(plan, dict)
        items = plan["items"]
        assert isinstance(items, list)
        items.append({
            "id": "verify",
            "deliverable": "Verification",
            "scope": "Only verification",
            "covers_ac": ["ac-1"],
            "evidence_plan": ["artifacts/verify.txt"],
            "actual_evidence": [],
            "depends_on": ["implement"],
            "status": "pending",
            "next_action": "Verify after implementation",
        })
        self.service.submit_plan(
            action["workspace"], "host/codex", action["state_revision"],
            action["action_token"], payload,
        )
        self.runtime.submission = {
            "updates": [{
                "item_id": "verify", "status": "completed",
                "actual_evidence": ["artifacts/verify.txt"],
            }],
            "command_results": [],
            "artifacts": [{"relative": "artifacts/verify.txt", "content": "verified"}],
        }

        review = self.service.run(None, self.repo, "host/codex")

        self.assertEqual(review["context"]["run"]["status"], "failed")
        workspace = self.store.resolve(str(review["workspace"]), self.repo)
        result = json.loads(
            (workspace / "runs" / review["context"]["run"]["run_id"] / "result.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("dependencies not completed", result["events"][-1]["error"])

    def test_run_without_a_configured_runtime_fails_before_mutating_ready_state(self) -> None:
        self.start_and_submit_plan()
        workspace = self.store.resolve(None, self.repo)
        before = self.store.load_state(workspace)
        service = TwinService(self.store, resources=self.service.resources)

        with self.assertRaisesRegex(ValueError, "worker runtime is not configured"):
            service.run(None, self.repo, "host/codex")

        self.assertEqual(self.store.load_state(workspace), before)

    def test_completion_requires_stored_evidence(self) -> None:
        self.start_and_submit_plan()
        self.runtime.submission = {
            "updates": [{
                "item_id": "implement", "status": "completed",
                "actual_evidence": ["artifacts/evidence.txt"],
            }],
            "command_results": [],
            "artifacts": [],
        }

        review = self.service.run(None, self.repo, "host/codex")

        self.assertEqual(review["context"]["run"]["status"], "failed")

    def test_accepted_completion_requires_evidence_for_each_criterion(self) -> None:
        self.start_and_submit_plan()
        self.runtime.complete_with_evidence()
        review = self.service.run(None, self.repo, "host/codex")
        result = self.service.submit_review(
            review["workspace"], "host/codex", review["state_revision"], review["action_token"],
            review["context"]["run"]["run_id"],
            {"decision": "accepted"},
        )
        self.assertEqual(result["status"], "accepted_done")

    def test_undeclared_stored_evidence_cannot_complete_an_acceptance_criterion(self) -> None:
        self.start_and_submit_plan()
        self.runtime.submission = {
            "updates": [{
                "item_id": "implement", "status": "completed",
                "actual_evidence": ["artifacts/undeclared.txt"],
            }],
            "command_results": [],
            "artifacts": [{"relative": "artifacts/undeclared.txt", "content": "verified"}],
        }

        review = self.service.run(None, self.repo, "host/codex")

        self.assertEqual(review["context"]["run"]["status"], "failed")

    def test_ac_bearing_item_with_an_empty_evidence_plan_cannot_become_ready(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        payload = valid_goal_and_plan()
        plan = payload["plan"]
        assert isinstance(plan, dict)
        items = plan["items"]
        assert isinstance(items, list)
        item = items[0]
        assert isinstance(item, dict)
        item["evidence_plan"] = []
        with self.assertRaisesRegex(ValueError, "evidence_plan is required"):
            self.service.submit_plan(
                action["workspace"], "host/codex", action["state_revision"],
                action["action_token"], payload,
            )

    def test_plan_commit_failure_leaves_documents_state_and_token_unchanged(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        workspace = self.store.resolve(str(action["workspace"]), self.repo)
        before = {
            name: (workspace / name).read_bytes()
            for name in ("goal.yaml", "plan.yaml", "state.json", "events.jsonl")
        }
        with patch.object(self.store, "_publish_staged", side_effect=OSError("injected"), create=True):
            with self.assertRaisesRegex(OSError, "injected"):
                self.service.submit_plan(
                    action["workspace"], "host/codex", action["state_revision"], action["action_token"], valid_goal_and_plan()
                )
        self.assertEqual({name: (workspace / name).read_bytes() for name in before}, before)
        self.assertEqual(
            self.service.submit_plan(
                action["workspace"], "host/codex", action["state_revision"], action["action_token"], valid_goal_and_plan()
            )["status"],
            "ready",
        )

    def test_stale_competing_plan_submission_cannot_overwrite_winner_documents(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        winner = valid_goal_and_plan()
        winner_goal = winner["goal"]
        assert isinstance(winner_goal, dict)
        winner_goal["one_liner"] = "Winner plan"
        self.service.submit_plan(action["workspace"], "host/codex", action["state_revision"], action["action_token"], winner)
        loser = valid_goal_and_plan()
        loser_goal = loser["goal"]
        assert isinstance(loser_goal, dict)
        loser_goal["one_liner"] = "Loser plan"
        with self.assertRaisesRegex(ValueError, "stale or consumed action"):
            self.service.submit_plan(action["workspace"], "host/codex", action["state_revision"], action["action_token"], loser)
        workspace = self.store.resolve(str(action["workspace"]), self.repo)
        self.assertIn("Winner plan", (workspace / "goal.yaml").read_text(encoding="utf-8"))
        self.assertNotIn("Loser plan", (workspace / "goal.yaml").read_text(encoding="utf-8"))

    def test_recovery_restores_old_workspace_after_child_dies_mid_publication(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        workspace = self.store.resolve(str(action["workspace"]), self.repo)
        before = {
            relative: (workspace / relative).read_bytes()
            for relative in ("goal.yaml", "plan.yaml", "state.json", "events.jsonl")
        }
        root = Path(self.tempdir.name)
        script = textwrap.dedent("""
            import json
            import os
            import sys
            from pathlib import Path
            from twin.domain.service import TwinService
            from twin.paths import TwinPaths
            from twin.resources import ResourceCatalog
            from twin.storage.workspaces import WorkspaceStore

            root = Path(sys.argv[1])
            action = json.loads(sys.argv[2])
            payload = json.loads(sys.argv[3])
            store = WorkspaceStore(TwinPaths.for_home(root / "home"))
            service = TwinService(store, resources=ResourceCatalog(Path(sys.argv[4])))

            def crash_after_first_publish(staged, previous, state_path, transaction_fd):
                target = next(target for target in staged if target != state_path)
                os.replace(staged[target].name, target, src_dir_fd=transaction_fd)
                os._exit(79)

            store._publish_staged = crash_after_first_publish
            service.submit_plan(
                action["workspace"], "host/codex", action["state_revision"], action["action_token"], payload
            )
        """)
        result = subprocess.run(
            [
                sys.executable, "-c", script, str(root), json.dumps(action),
                json.dumps(valid_goal_and_plan()), str(Path(__file__).resolve().parents[1]),
            ],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            check=False,
        )
        self.assertEqual(result.returncode, 79)
        self.assertEqual(self.store.load_state(workspace)["state_revision"], 1)
        self.assertEqual({relative: (workspace / relative).read_bytes() for relative in before}, before)
        self.assertEqual(
            self.service.submit_plan(
                action["workspace"], "host/codex", action["state_revision"], action["action_token"], valid_goal_and_plan()
            )["status"],
            "ready",
        )

    def test_staging_second_file_failure_leaves_no_transaction_temps(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        workspace = self.store.resolve(str(action["workspace"]), self.repo)
        original_stage = self.store._stage_bytes
        calls = 0

        def fail_second_stage(transaction_fd: int, name: str, body: bytes) -> str:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("second staging failure")
            return original_stage(transaction_fd, name, body)

        with patch.object(self.store, "_stage_bytes", side_effect=fail_second_stage):
            with self.assertRaisesRegex(OSError, "second staging failure"):
                self.service.submit_plan(
                    action["workspace"], "host/codex", action["state_revision"], action["action_token"], valid_goal_and_plan()
                )
        self.assertEqual(list(workspace.rglob("*.tmp")), [])
        self.assertFalse((workspace / ".transactions").exists())

    def test_round_four_foreign_root_adoption_is_obsolete_and_retryable(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        workspace = self.store.resolve(str(action["workspace"]), self.repo)
        before_workspace = self.workspace_snapshot(workspace)
        foreign_root = Path(self.tempdir.name) / "foreign-transaction-root"
        foreign_root.mkdir()
        (foreign_root / "foreign-marker").write_bytes(b"foreign tree")
        before_foreign = tree_snapshot(foreign_root)
        held_root = workspace / ".transactions-held"
        transaction_root = workspace / ".transactions"
        original_mkdir = os.mkdir
        original_stage = self.store._stage_bytes
        stage_calls = 0

        def replace_created_root(path: object, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
            if dir_fd is None:
                original_mkdir(path, mode)
            else:
                original_mkdir(path, mode, dir_fd=dir_fd)
            if dir_fd is not None and path == ".transactions":
                transaction_root.rename(held_root)
                foreign_root.rename(transaction_root)

        def fail_second_stage(descriptor: int, name: str, body: bytes) -> str:
            nonlocal stage_calls
            stage_calls += 1
            if stage_calls == 2:
                raise OSError("second staging failure")
            return original_stage(descriptor, name, body)

        with patch("twin.storage.workspaces.os.mkdir", side_effect=replace_created_root):
            with patch.object(self.store, "_stage_bytes", side_effect=fail_second_stage):
                with self.assertRaisesRegex(OSError, "second staging failure"):
                    self.service.submit_plan(
                        action["workspace"], "host/codex", action["state_revision"],
                        action["action_token"], valid_goal_and_plan(),
                    )

        self.assertEqual(tree_snapshot(foreign_root), before_foreign)
        self.assertEqual(self.workspace_snapshot(workspace), before_workspace)
        self.assertFalse((workspace / ".transaction.json").exists())
        self.assertFalse((workspace / ".transactions").exists())
        self.assertEqual(
            self.service.submit_plan(
                action["workspace"], "host/codex", action["state_revision"],
                action["action_token"], valid_goal_and_plan(),
            )["status"],
            "ready",
        )

    def test_round_four_child_collision_leaves_no_orphan_root_and_retry_works(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        workspace = self.store.resolve(str(action["workspace"]), self.repo)
        before_workspace = self.workspace_snapshot(workspace)
        transaction_id = "a" * 32
        transaction_root = workspace / ".transactions"
        original_mkdir = os.mkdir

        def inject_child_collision(path: object, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
            if dir_fd is None:
                original_mkdir(path, mode)
            else:
                original_mkdir(path, mode, dir_fd=dir_fd)
            if dir_fd is not None and path == ".transactions":
                original_mkdir(transaction_root / transaction_id)

        with patch("twin.storage.workspaces.secrets.token_hex", return_value=transaction_id):
            with patch("twin.storage.workspaces.os.mkdir", side_effect=inject_child_collision):
                with patch.object(
                    self.store, "_stage_bytes", side_effect=OSError("flat stage blocked")
                ):
                    with self.assertRaises((OSError, ValueError)):
                        self.service.submit_plan(
                            action["workspace"], "host/codex", action["state_revision"],
                            action["action_token"], valid_goal_and_plan(),
                        )

        self.assertEqual(self.workspace_snapshot(workspace), before_workspace)
        self.assertFalse((workspace / ".transaction.json").exists())
        self.assertFalse((workspace / ".transactions").exists())
        self.assertEqual(
            self.service.submit_plan(
                action["workspace"], "host/codex", action["state_revision"],
                action["action_token"], valid_goal_and_plan(),
            )["status"],
            "ready",
        )

    def test_predicted_stage_symlink_or_file_fails_before_action_mutation(self) -> None:
        transaction_id = "b" * 32
        for kind in ("file", "symlink"):
            with self.subTest(kind=kind):
                action = self.service.start(f"ship {kind}", self.repo, "host/codex")
                workspace = self.store.resolve(str(action["workspace"]), self.repo)
                before_workspace = self.workspace_snapshot(workspace)
                stage = workspace / transaction_stage_name(transaction_id, 0)
                outside = Path(self.tempdir.name) / f"predicted-{kind}-outside"
                outside.mkdir()
                (outside / "sentinel.bin").write_bytes(b"outside tree")
                before_outside = tree_snapshot(outside)
                if kind == "file":
                    stage.write_bytes(b"foreign stage entry")
                else:
                    stage.symlink_to(outside / "sentinel.bin")

                with patch(
                    "twin.storage.workspaces.secrets.token_hex", return_value=transaction_id
                ):
                    with self.assertRaises((OSError, ValueError)):
                        self.service.submit_plan(
                            action["workspace"], "host/codex", action["state_revision"],
                            action["action_token"], valid_goal_and_plan(),
                        )

                self.assertEqual(tree_snapshot(outside), before_outside)
                self.assertEqual(self.workspace_snapshot(workspace), before_workspace)
                self.assertFalse((workspace / ".transaction.json").exists())
                if kind == "file":
                    self.assertEqual(stage.read_bytes(), b"foreign stage entry")
                else:
                    self.assertTrue(stage.is_symlink())
                stage.unlink()
                self.assertEqual(
                    self.service.submit_plan(
                        action["workspace"], "host/codex", action["state_revision"],
                        action["action_token"], valid_goal_and_plan(),
                    )["status"],
                    "ready",
                )

    def test_replaced_stage_name_cannot_redirect_publication(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        workspace = self.store.resolve(str(action["workspace"]), self.repo)
        before_workspace = self.workspace_snapshot(workspace)
        outside = Path(self.tempdir.name) / "stage-replacement-outside"
        outside.mkdir()
        (outside / "sentinel.bin").write_bytes(b"outside tree")
        before_outside = tree_snapshot(outside)
        original_publish = self.store._publish_staged
        held_stage: Path | None = None
        replacement: Path | None = None

        def replace_stage_before_publish(*args: object) -> None:
            nonlocal held_stage, replacement
            staged = args[0]
            assert isinstance(staged, dict)
            value = next(iter(staged.values()))
            name = value if isinstance(value, str) else value.name
            candidate = workspace / name
            if candidate.is_file():
                held_stage = workspace / f"{name}.held"
                replacement = candidate
                candidate.rename(held_stage)
                candidate.symlink_to(outside / "sentinel.bin")
            original_publish(*args)

        with patch.object(self.store, "_publish_staged", side_effect=replace_stage_before_publish):
            with self.assertRaises((OSError, ValueError)):
                self.service.submit_plan(
                    action["workspace"], "host/codex", action["state_revision"],
                    action["action_token"], valid_goal_and_plan(),
                )

        self.assertIsNotNone(held_stage)
        self.assertIsNotNone(replacement)
        self.assertEqual(tree_snapshot(outside), before_outside)
        self.assertEqual(self.workspace_snapshot(workspace), before_workspace)
        self.assertTrue((workspace / ".transaction.json").is_file())
        assert held_stage is not None and replacement is not None
        replacement.unlink()
        held_stage.rename(replacement)
        self.assertEqual(self.store.load_state(workspace)["state_revision"], action["state_revision"])
        self.assertFalse((workspace / ".transaction.json").exists())
        self.assertEqual(
            self.service.submit_plan(
                action["workspace"], "host/codex", action["state_revision"],
                action["action_token"], valid_goal_and_plan(),
            )["status"],
            "ready",
        )

    def test_unowned_stage_namespace_entry_is_preserved_and_fails_closed(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        workspace = self.store.resolve(str(action["workspace"]), self.repo)
        before_workspace = self.workspace_snapshot(workspace)
        foreign_stage = workspace / transaction_stage_name("f" * 32, 99)
        foreign_stage.write_bytes(b"unowned stage")

        with self.assertRaisesRegex(ValueError, "unexpected transaction stage"):
            self.service.submit_plan(
                action["workspace"], "host/codex", action["state_revision"],
                action["action_token"], valid_goal_and_plan(),
            )

        self.assertEqual(foreign_stage.read_bytes(), b"unowned stage")
        self.assertEqual(self.workspace_snapshot(workspace), before_workspace)
        self.assertFalse((workspace / ".transaction.json").exists())
        foreign_stage.unlink()
        self.assertEqual(
            self.service.submit_plan(
                action["workspace"], "host/codex", action["state_revision"],
                action["action_token"], valid_goal_and_plan(),
            )["status"],
            "ready",
        )

    def test_commit_action_reuses_one_verified_workspace_descriptor(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        workspace = self.store.resolve(str(action["workspace"]), self.repo)
        current = self.store.load_state(workspace)

        with patch.object(
            self.store,
            "_open_workspace_directory",
            wraps=self.store._open_workspace_directory,
        ) as open_workspace:
            self.store.commit_action(
                workspace,
                action["state_revision"],
                current,
                documents={},
                artifacts={},
                event=None,
                validate_current=lambda value: None,
            )

        self.assertEqual(open_workspace.call_count, 1)

    def test_stage_not_owned_by_current_journal_is_preserved_and_blocks_recovery(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        workspace = self.store.resolve(str(action["workspace"]), self.repo)
        before_workspace = self.workspace_snapshot(workspace)
        foreign_stage = workspace / transaction_stage_name("f" * 32, 99)

        def inject_foreign_stage(*args: object) -> None:
            foreign_stage.write_bytes(b"not journal-owned")
            raise OSError("publication blocked")

        with patch("twin.storage.workspaces.secrets.token_hex", return_value="a" * 32):
            with patch.object(
                self.store, "_publish_staged", side_effect=inject_foreign_stage
            ):
                with self.assertRaisesRegex(ValueError, "invalid transaction journal"):
                    self.service.submit_plan(
                        action["workspace"], "host/codex", action["state_revision"],
                        action["action_token"], valid_goal_and_plan(),
                    )

        self.assertEqual(foreign_stage.read_bytes(), b"not journal-owned")
        self.assertEqual(self.workspace_snapshot(workspace), before_workspace)
        self.assertTrue((workspace / ".transaction.json").is_file())
        foreign_stage.unlink()
        self.assertEqual(self.store.load_state(workspace)["state_revision"], action["state_revision"])
        self.assertFalse((workspace / ".transaction.json").exists())
        self.assertEqual(
            self.service.submit_plan(
                action["workspace"], "host/codex", action["state_revision"],
                action["action_token"], valid_goal_and_plan(),
            )["status"],
            "ready",
        )

    def test_journal_link_failure_removes_owned_journal_stage_and_retry_succeeds(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        workspace = self.store.resolve(str(action["workspace"]), self.repo)
        transaction_id = "c" * 32

        def fail_journal_link(*args: object, **kwargs: object) -> None:
            raise OSError("journal link failure")

        with patch("twin.storage.workspaces.secrets.token_hex", return_value=transaction_id):
            with patch("twin.storage.workspaces.os.link", side_effect=fail_journal_link):
                with self.assertRaisesRegex(OSError, "journal link failure"):
                    self.service.submit_plan(
                        action["workspace"], "host/codex", action["state_revision"],
                        action["action_token"], valid_goal_and_plan(),
                    )

        retry_failure: BaseException | None = None
        try:
            retry = self.service.submit_plan(
                action["workspace"], "host/codex", action["state_revision"],
                action["action_token"], valid_goal_and_plan(),
            )
        except BaseException as exc:
            retry_failure = exc
            retry = None
        owned_stages = sorted(
            entry.name for entry in workspace.iterdir()
            if entry.name == transaction_journal_stage_name(transaction_id)
        )
        self.assertEqual(
            owned_stages, [],
            f"retry failed with {retry_failure!r}; leaked journal stages: {owned_stages}",
        )
        if retry_failure is not None:
            raise retry_failure
        assert retry is not None
        self.assertEqual(retry["status"], "ready")

    def test_foreign_journal_race_preserves_foreign_entries_and_removes_owned_stages(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        workspace = self.store.resolve(str(action["workspace"]), self.repo)
        transaction_id = "d" * 32
        foreign_stage = workspace / transaction_stage_name("f" * 32, 99)
        foreign_stage_body = b"foreign stage"
        foreign_journal_body = b"foreign journal"

        def race_foreign_journal(
            src: object,
            dst: object,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
            follow_symlinks: bool = True,
        ) -> None:
            del src, follow_symlinks
            self.assertEqual(dst, ".transaction.json")
            assert dst_dir_fd is not None
            descriptor = os.open(
                ".transaction.json",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=dst_dir_fd,
            )
            try:
                os.write(descriptor, foreign_journal_body)
            finally:
                os.close(descriptor)
            stage_descriptor = os.open(
                foreign_stage.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=dst_dir_fd,
            )
            try:
                os.write(stage_descriptor, foreign_stage_body)
            finally:
                os.close(stage_descriptor)
            raise FileExistsError("foreign journal raced")

        with patch("twin.storage.workspaces.secrets.token_hex", return_value=transaction_id):
            with patch("twin.storage.workspaces.os.link", side_effect=race_foreign_journal):
                with self.assertRaises(ValueError):
                    self.service.submit_plan(
                        action["workspace"], "host/codex", action["state_revision"],
                        action["action_token"], valid_goal_and_plan(),
                    )

        self.assertEqual((workspace / ".transaction.json").read_bytes(), foreign_journal_body)
        self.assertEqual(foreign_stage.read_bytes(), foreign_stage_body)
        owned_stages = sorted(
            entry.name for entry in workspace.iterdir()
            if entry.name.startswith(f".twin-txn-{transaction_id}-")
        )
        self.assertEqual(owned_stages, [])
        (workspace / ".transaction.json").unlink()
        foreign_stage.unlink()
        self.assertEqual(
            self.service.submit_plan(
                action["workspace"], "host/codex", action["state_revision"],
                action["action_token"], valid_goal_and_plan(),
            )["status"],
            "ready",
        )

    def test_respond_publish_failure_leaves_no_artifact_event_or_state_drift(self) -> None:
        human = self._needs_human_workspace()
        workspace = self.store.resolve(str(human["workspace"]), self.repo)
        before = {
            relative: (workspace / relative).read_bytes()
            for relative in ("state.json", "events.jsonl")
        }
        with patch.object(self.store, "_publish_staged", side_effect=OSError("respond publish failure")):
            with self.assertRaisesRegex(OSError, "respond publish failure"):
                self.service.respond(human["workspace"], self.repo, "sensitive approval")
        self.assertEqual({relative: (workspace / relative).read_bytes() for relative in before}, before)
        self.assertFalse((workspace / "artifacts" / "human").exists())
        self.assertEqual(self.service.respond(human["workspace"], self.repo, "sensitive approval")["status"], "ready")

    def test_handoff_publish_failure_leaves_route_and_event_stream_coherent(self) -> None:
        ready = self.start_and_submit_plan()
        workspace = self.store.resolve(str(ready["workspace"]), self.repo)
        before = {
            relative: (workspace / relative).read_bytes()
            for relative in ("state.json", "events.jsonl")
        }
        with patch.object(self.store, "_publish_staged", side_effect=OSError("handoff publish failure")):
            with self.assertRaisesRegex(OSError, "handoff publish failure"):
                self.service.handoff(ready["workspace"], self.repo, "host/codex", "host/claude")
        self.assertEqual({relative: (workspace / relative).read_bytes() for relative in before}, before)
        self.assertEqual(
            self.service.handoff(ready["workspace"], self.repo, "host/codex", "host/claude")["supervisor_route"],
            "host/claude",
        )

    def test_journal_transaction_id_cannot_escape_stage_namespace(self) -> None:
        cases = []
        absolute_victim = Path(self.tempdir.name) / "absolute-victim"
        absolute_victim.mkdir()
        cases.append((str(absolute_victim), absolute_victim))
        action = self.service.start("ship feature", self.repo, "host/codex")
        workspace = self.store.resolve(str(action["workspace"]), self.repo)
        traversal_victim = workspace / "victim"
        traversal_victim.mkdir()
        cases.append(("../victim", traversal_victim))
        workspace_status = workspace.stat()
        for transaction_id, victim in cases:
            with self.subTest(transaction_id=transaction_id):
                (workspace / ".transaction.json").write_text(
                    json.dumps({
                        "schema_version": 3,
                        "transaction_id": transaction_id,
                        "workspace_identity": {
                            "device": workspace_status.st_dev,
                            "inode": workspace_status.st_ino,
                        },
                        "journal_stage_name": f".twin-txn-{transaction_id}-journal.stage",
                        "stage_files": [],
                        "targets": [],
                        "created_directories": [],
                    }),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "invalid transaction journal"):
                    self.store.load_state(workspace)
                self.assertTrue(victim.is_dir())
                self.assertTrue((workspace / ".transaction.json").is_file())

    def test_malformed_or_symlinked_journal_transaction_ids_fail_closed(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        workspace = self.store.resolve(str(action["workspace"]), self.repo)
        workspace_status = workspace.stat()
        for transaction_id in ("g" * 32, "a" * 31):
            with self.subTest(transaction_id=transaction_id):
                (workspace / ".transaction.json").write_text(
                    json.dumps({
                        "schema_version": 3,
                        "transaction_id": transaction_id,
                        "workspace_identity": {
                            "device": workspace_status.st_dev,
                            "inode": workspace_status.st_ino,
                        },
                        "journal_stage_name": f".twin-txn-{transaction_id}-journal.stage",
                        "stage_files": [],
                        "targets": [],
                        "created_directories": [],
                    }),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "invalid transaction journal"):
                    self.store.load_state(workspace)
                self.assertTrue((workspace / ".transaction.json").is_file())
        transaction_id = "a" * 32
        victim = Path(self.tempdir.name) / "symlink-victim"
        victim.write_bytes(b"outside")
        stage_name = transaction_stage_name(transaction_id, 0)
        stage = workspace / stage_name
        stage.symlink_to(victim)
        stage_status = stage.lstat()
        (workspace / ".transaction.json").write_text(
            json.dumps({
                "schema_version": 3,
                "transaction_id": transaction_id,
                "workspace_identity": {
                    "device": workspace_status.st_dev,
                    "inode": workspace_status.st_ino,
                },
                "journal_stage_name": f".twin-txn-{transaction_id}-journal.stage",
                "stage_files": [{
                    "name": stage_name,
                    "device": stage_status.st_dev,
                    "inode": stage_status.st_ino,
                }],
                "targets": [{"relative": "goal.yaml", "before": None}],
                "created_directories": [],
            }),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "invalid transaction journal"):
            self.store.load_state(workspace)
        self.assertTrue(victim.is_file())
        self.assertTrue((workspace / ".transaction.json").is_file())

    def test_pre_release_transaction_journals_fail_closed(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        workspace = self.store.resolve(str(action["workspace"]), self.repo)
        for schema_version in (1, 2):
            with self.subTest(schema_version=schema_version):
                (workspace / ".transaction.json").write_text(
                    json.dumps({
                        "schema_version": schema_version,
                        "transaction_id": "a" * 32,
                        "targets": [],
                        "created_directories": [],
                    }),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "invalid transaction journal"):
                    self.store.load_state(workspace)
                self.assertTrue((workspace / ".transaction.json").is_file())

    def test_artifact_writes_cannot_use_transaction_metadata_paths(self) -> None:
        stage = transaction_stage_name("a" * 32, 0)
        for relative in (
            ".transaction.json",
            ".transactions",
            ".transactions/stage.bin",
            stage,
            f"artifacts/{stage}",
        ):
            with self.subTest(relative=relative):
                action = self.service.start("ship feature", self.repo, "host/codex")
                workspace = self.store.resolve(str(action["workspace"]), self.repo)
                with self.assertRaisesRegex(ValueError, "artifact path is reserved"):
                    self.store.write_artifact(workspace, relative, b"blocked")

    def test_journal_cleanup_failure_retains_journal_for_later_recovery(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        workspace = self.store.resolve(str(action["workspace"]), self.repo)
        before = {name: (workspace / name).read_bytes() for name in ("goal.yaml", "plan.yaml", "state.json", "events.jsonl")}
        original_unlink = self.store._unlink_stage_entry

        def retain_journal(workspace_fd: int, name: str) -> None:
            if name == ".transaction.json":
                return
            original_unlink(workspace_fd, name)

        with patch.object(self.store, "_unlink_stage_entry", side_effect=retain_journal):
            with self.assertRaisesRegex(OSError, "transaction staging cleanup"):
                self.service.submit_plan(
                    action["workspace"], "host/codex", action["state_revision"], action["action_token"], valid_goal_and_plan()
                )
        self.assertTrue((workspace / ".transaction.json").is_file())
        self.assertFalse((workspace / ".transactions").exists())
        self.assertEqual(self.store.load_state(workspace)["state_revision"], 1)
        self.assertEqual({name: (workspace / name).read_bytes() for name in before}, before)
        self.assertFalse((workspace / ".transaction.json").exists())

    def test_created_directory_cleanup_failure_retains_journal_for_retry(self) -> None:
        human = self._needs_human_workspace()
        workspace = self.store.resolve(str(human["workspace"]), self.repo)
        digest = hashlib.sha256(b"sensitive approval").hexdigest()
        created = workspace / "artifacts" / "human"
        original_rmdir = self.store._remove_created_directory

        def fail_created_directory(parent_fd: int, name: str) -> None:
            if name == created.name:
                raise OSError("created directory blocked")
            original_rmdir(parent_fd, name)

        original_replace = os.replace
        publish_calls = 0

        def fail_second_publish(*args: object, **kwargs: object) -> None:
            nonlocal publish_calls
            publish_calls += 1
            if publish_calls == 2:
                raise OSError("publish blocked")
            original_replace(*args, **kwargs)

        with patch("twin.storage.workspaces.os.replace", side_effect=fail_second_publish):
            with patch.object(
                self.store, "_remove_created_directory", side_effect=fail_created_directory
            ):
                with self.assertRaisesRegex(OSError, "created directory blocked"):
                    self.service.respond(human["workspace"], self.repo, "sensitive approval")
        self.assertTrue((workspace / ".transaction.json").is_file())
        self.assertTrue(created.is_dir())
        self.assertEqual(self.store.load_state(workspace)["status"], "needs_human")
        self.assertFalse((workspace / ".transaction.json").exists())
        self.assertFalse(created.exists())
        self.assertFalse((workspace / "artifacts" / "human" / f"{digest}.txt").exists())

    def test_terminal_workspace_cannot_mutate(self) -> None:
        self.start_and_submit_plan()
        self.runtime.complete_with_evidence()
        review = self.service.run(None, self.repo, "host/codex")
        self.service.submit_review(
            review["workspace"], "host/codex", review["state_revision"],
            review["action_token"], review["context"]["run"]["run_id"],
            {"decision": "accepted"},
        )
        with self.assertRaisesRegex(ValueError, "terminal workspace"):
            self.service.run(review["workspace"], self.repo, "host/codex")

    def test_handoff_rejects_pending_action(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        with self.assertRaisesRegex(ValueError, "pending action"):
            self.service.handoff(action["workspace"], self.repo, "host/codex", "host/claude")

    def test_response_requires_needs_human_state(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        with self.assertRaisesRegex(ValueError, "workspace is not awaiting human response"):
            self.service.respond(action["workspace"], self.repo, "approved")

    def test_status_rejects_inconsistent_event_workspace_id(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        workspace = self.store.resolve(str(action["workspace"]), self.repo)
        with (workspace / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"workspace_id": "other-workspace"}) + "\n")
        with self.assertRaisesRegex(ValueError, "event workspace_id mismatch"):
            self.service.status(action["workspace"], self.repo)

    def test_respond_writes_hash_named_artifact_without_answer_in_event(self) -> None:
        human = self._needs_human_workspace()
        result = self.service.respond(human["workspace"], self.repo, "sensitive approval")
        body = b"sensitive approval"
        workspace = self.store.resolve(str(human["workspace"]), self.repo)
        expected = "artifacts/human/" + hashlib.sha256(body).hexdigest() + ".txt"
        self.assertEqual(result["artifact"]["relative"], expected)
        events = (workspace / "events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("sensitive approval", events)

    def _needs_human_workspace(self) -> dict[str, object]:
        self.start_and_submit_plan()
        review = self.service.run(None, self.repo, "host/codex")
        return self.service.submit_review(
            review["workspace"], "host/codex", review["state_revision"], review["action_token"],
            review["context"]["run"]["run_id"], {"decision": "needs_human"},
        )


class _DomainRuntime:
    def __init__(self) -> None:
        self.submission: dict[str, object] = {
            "updates": [],
            "command_results": [],
            "artifacts": [],
        }

    def complete_with_evidence(self) -> None:
        self.submission = {
            "updates": [{
                "item_id": "implement", "status": "completed",
                "actual_evidence": ["artifacts/evidence.txt"],
            }],
            "command_results": [],
            "artifacts": [{"relative": "artifacts/evidence.txt", "content": "verified"}],
        }

    def run_turn(self, request: WorkerTurnRequest) -> WorkerTurnResult:
        return WorkerTurnResult(
            output_text="domain test worker",
            returncode=0,
            session_id="domain-test",
            events=({"event": "completed", "provider": request.provider},),
            submission=self.submission,
        )
