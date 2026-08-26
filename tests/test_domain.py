import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import textwrap
from unittest.mock import patch
from unittest import TestCase

from twin.domain.service import TwinService
from twin.paths import TwinPaths
from twin.storage.workspaces import WorkspaceStore


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


class TwinServiceTest(TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        self.store = WorkspaceStore(TwinPaths.for_home(root / "home"))
        self.service = TwinService(self.store)

    def start_and_submit_plan(self) -> dict[str, object]:
        action = self.service.start("ship feature", self.repo, "host/codex")
        return self.service.submit_plan(
            action["workspace"], "host/codex", action["state_revision"], action["action_token"], valid_goal_and_plan()
        )

    def start_run_and_submit_instruction(self, updates: list[dict[str, object]]) -> dict[str, object]:
        self.start_and_submit_plan()
        action = self.service.run(None, self.repo, "host/codex")
        run_id = action["context"]["metadata"]["run_id"]
        return self.service.submit_instruction(
            action["workspace"], "host/codex", action["state_revision"], action["action_token"],
            run_id, {"updates": updates},
        )

    def test_start_returns_author_plan_action(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        self.assertEqual(action["action"], "author_plan")
        self.assertEqual(action["state_revision"], 1)
        self.assertIn("submit-plan", action["submit"]["command"])

    def test_run_action_identifies_the_runnable_plan_item(self) -> None:
        self.start_and_submit_plan()
        action = self.service.run(None, self.repo, "host/codex")
        self.assertEqual(action["context"]["metadata"]["item_id"], "implement")

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
        self.service.submit_plan(action["workspace"], "host/codex", action["state_revision"], action["action_token"], payload)
        run = self.service.run(None, self.repo, "host/codex")
        workspace = self.store.resolve(str(run["workspace"]), self.repo)
        self.store.write_artifact(workspace, "artifacts/verify.txt", b"verified")
        with self.assertRaisesRegex(ValueError, "dependencies not completed"):
            self.service.submit_instruction(
                run["workspace"], "host/codex", run["state_revision"], run["action_token"], run["context"]["metadata"]["run_id"],
                {"updates": [{"item_id": "verify", "status": "completed", "actual_evidence": ["artifacts/verify.txt"]}]},
            )

    def test_wrong_run_id_cannot_submit_instruction(self) -> None:
        self.start_and_submit_plan()
        action = self.service.run(None, self.repo, "host/codex")
        with self.assertRaisesRegex(ValueError, "run ID mismatch"):
            self.service.submit_instruction(
                action["workspace"], "host/codex", action["state_revision"], action["action_token"],
                "wrong-run", {"updates": []},
            )

    def test_completion_requires_stored_evidence(self) -> None:
        self.start_and_submit_plan()
        run = self.service.run(None, self.repo, "host/codex")
        with self.assertRaisesRegex(ValueError, "missing evidence"):
            self.service.submit_instruction(
                run["workspace"], "host/codex", run["state_revision"], run["action_token"], run["context"]["metadata"]["run_id"],
                {"updates": [{"item_id": "implement", "status": "completed", "actual_evidence": ["artifacts/evidence.txt"]}]},
            )

    def test_accepted_completion_requires_evidence_for_each_criterion(self) -> None:
        self.start_and_submit_plan()
        run = self.service.run(None, self.repo, "host/codex")
        workspace = self.store.resolve(str(run["workspace"]), self.repo)
        self.store.write_artifact(workspace, "artifacts/evidence.txt", b"verified")
        review = self.service.submit_instruction(
            run["workspace"], "host/codex", run["state_revision"], run["action_token"], run["context"]["metadata"]["run_id"],
            {"updates": [
            {"item_id": "implement", "status": "completed", "actual_evidence": ["artifacts/evidence.txt"]},
            ]},
        )
        result = self.service.submit_review(
            review["workspace"], "host/codex", review["state_revision"], review["action_token"], review["context"]["metadata"]["run_id"],
            {"decision": "accepted"},
        )
        self.assertEqual(result["status"], "accepted_done")

    def test_undeclared_stored_evidence_cannot_complete_an_acceptance_criterion(self) -> None:
        self.start_and_submit_plan()
        run = self.service.run(None, self.repo, "host/codex")
        workspace = self.store.resolve(str(run["workspace"]), self.repo)
        self.store.write_artifact(workspace, "artifacts/undeclared.txt", b"verified")
        with self.assertRaisesRegex(ValueError, "undeclared evidence"):
            self.service.submit_instruction(
                run["workspace"], "host/codex", run["state_revision"], run["action_token"],
                run["context"]["metadata"]["run_id"],
                {"updates": [{"item_id": "implement", "status": "completed", "actual_evidence": ["artifacts/undeclared.txt"]}]},
            )

    def test_ac_bearing_item_with_an_empty_evidence_plan_cannot_complete(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        payload = valid_goal_and_plan()
        plan = payload["plan"]
        assert isinstance(plan, dict)
        items = plan["items"]
        assert isinstance(items, list)
        item = items[0]
        assert isinstance(item, dict)
        item["evidence_plan"] = []
        self.service.submit_plan(action["workspace"], "host/codex", action["state_revision"], action["action_token"], payload)
        run = self.service.run(None, self.repo, "host/codex")
        with self.assertRaisesRegex(ValueError, "missing evidence"):
            self.service.submit_instruction(
                run["workspace"], "host/codex", run["state_revision"], run["action_token"],
                run["context"]["metadata"]["run_id"],
                {"updates": [{"item_id": "implement", "status": "completed", "actual_evidence": []}]},
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
            from twin.storage.workspaces import WorkspaceStore

            root = Path(sys.argv[1])
            action = json.loads(sys.argv[2])
            payload = json.loads(sys.argv[3])
            store = WorkspaceStore(TwinPaths.for_home(root / "home"))
            service = TwinService(store)

            def crash_after_first_publish(staged, previous, state_path):
                target = next(target for target in staged if target != state_path)
                os.replace(staged[target], target)
                os._exit(79)

            store._publish_staged = crash_after_first_publish
            service.submit_plan(
                action["workspace"], "host/codex", action["state_revision"], action["action_token"], payload
            )
        """)
        result = subprocess.run(
            [sys.executable, "-c", script, str(root), json.dumps(action), json.dumps(valid_goal_and_plan())],
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

        def fail_second_stage(target: Path, body: bytes) -> Path:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("second staging failure")
            return original_stage(target, body)

        with patch.object(self.store, "_stage_bytes", side_effect=fail_second_stage):
            with self.assertRaisesRegex(OSError, "second staging failure"):
                self.service.submit_plan(
                    action["workspace"], "host/codex", action["state_revision"], action["action_token"], valid_goal_and_plan()
                )
        self.assertEqual(list(workspace.rglob("*.tmp")), [])
        self.assertFalse((workspace / ".transactions").exists())

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

    def test_terminal_workspace_cannot_mutate(self) -> None:
        action = self.service.start("ship feature", self.repo, "host/codex")
        workspace = self.store.resolve(str(action["workspace"]), self.repo)
        state = self.store.load_state(workspace)
        state["status"] = "accepted_done"
        state["pending_action"] = None
        self.store.replace_state(workspace, action["state_revision"], state)
        with self.assertRaisesRegex(ValueError, "terminal workspace"):
            self.service.run(action["workspace"], self.repo, "host/codex")

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
        self.start_and_submit_plan()
        run = self.service.run(None, self.repo, "host/codex")
        review = self.service.submit_instruction(
            run["workspace"], "host/codex", run["state_revision"], run["action_token"], run["context"]["metadata"]["run_id"], {"updates": []}
        )
        human = self.service.submit_review(
            review["workspace"], "host/codex", review["state_revision"], review["action_token"], review["context"]["metadata"]["run_id"],
            {"decision": "needs_human"},
        )
        result = self.service.respond(human["workspace"], self.repo, "sensitive approval")
        body = b"sensitive approval"
        workspace = self.store.resolve(str(human["workspace"]), self.repo)
        expected = "artifacts/human/" + hashlib.sha256(body).hexdigest() + ".txt"
        self.assertEqual(result["artifact"]["relative"], expected)
        events = (workspace / "events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("sensitive approval", events)

    def _needs_human_workspace(self) -> dict[str, object]:
        self.start_and_submit_plan()
        run = self.service.run(None, self.repo, "host/codex")
        review = self.service.submit_instruction(
            run["workspace"], "host/codex", run["state_revision"], run["action_token"],
            run["context"]["metadata"]["run_id"], {"updates": []},
        )
        return self.service.submit_review(
            review["workspace"], "host/codex", review["state_revision"], review["action_token"],
            review["context"]["metadata"]["run_id"], {"decision": "needs_human"},
        )
