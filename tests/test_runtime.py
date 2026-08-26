from __future__ import annotations

import json
import importlib
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from tempfile import TemporaryDirectory
from threading import Event, Thread
from typing import Any
from unittest import TestCase
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from twin.domain.service import TwinService
from twin.paths import TwinPaths
from twin.resources import ResourceCatalog
from twin.runtime.cao import CaoRuntime
from twin.runtime.local_cli import LocalCliRuntime
from twin.runtime.process import ProcessResult, ProcessRunner
from twin.runtime.protocols import WorkerTurnRequest, WorkerTurnResult
from twin.runtime.worktree import GitWorkspaceIsolation
from twin.schema import validate_document
from twin.storage.workspaces import WorkspaceStore
from twin.yaml_codec import load_yaml

from tests.test_domain import valid_goal_and_plan


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def init_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    git(repo, "init")
    git(repo, "config", "user.email", "twin-tests@example.invalid")
    git(repo, "config", "user.name", "Twin Tests")
    (repo / "README.md").write_text(f"# {repo.name}\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")


class RuntimeAdapterTest(TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.fake_provider = Path(__file__).parent / "fixtures" / "fake_provider.py"

    def test_worker_request_preserves_explicit_environment(self) -> None:
        request = WorkerTurnRequest(
            prompt="do work",
            cwd=Path("/tmp/repo"),
            provider="codex",
            session_id="",
            timeout_seconds=30,
            environment={"KEEP": "1"},
        )
        self.assertEqual(request.environment["KEEP"], "1")

    def test_local_cli_excludes_unapproved_host_environment(self) -> None:
        runtime = LocalCliRuntime(executables={
            "codex": [sys.executable, str(self.fake_provider), "env"],
        })
        request = WorkerTurnRequest(
            prompt="do work",
            cwd=self.root,
            provider="codex",
            session_id="session-in",
            timeout_seconds=5,
            environment={"VISIBLE": "yes"},
        )
        old_host_only = os.environ.get("HOST_ONLY")
        os.environ["HOST_ONLY"] = "do-not-forward"
        try:
            result = runtime.run_turn(request)
        finally:
            if old_host_only is None:
                os.environ.pop("HOST_ONLY", None)
            else:
                os.environ["HOST_ONLY"] = old_host_only
        payload = json.loads(result.output_text)
        self.assertEqual(payload, {"HOST_ONLY": None, "VISIBLE": "yes"})

    def test_local_cli_reports_missing_provider_without_running_shell(self) -> None:
        result = LocalCliRuntime(executables={
            "codex": [str(self.root / "missing-codex")],
        }).run_turn(WorkerTurnRequest(
            prompt="do work",
            cwd=self.root,
            provider="codex",
            session_id="",
            timeout_seconds=5,
        ))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.events[0]["failure_kind"], "provider_not_found")

    def test_local_cli_rejects_malformed_provider_json(self) -> None:
        runtime = LocalCliRuntime(executables={
            "codex": [sys.executable, str(self.fake_provider), "malformed"],
        })
        result = runtime.run_turn(WorkerTurnRequest(
            prompt="do work",
            cwd=self.root,
            provider="codex",
            session_id="",
            timeout_seconds=5,
        ))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.events[0]["failure_kind"], "malformed_output")
        self.assertIn("malformed provider output", result.output_text)

    def test_local_cli_rejects_empty_provider_output(self) -> None:
        runtime = LocalCliRuntime(
            executables={"codex": ["codex"]},
            process_runner=_StaticProcessRunner(stdout="", returncode=0),
        )
        result = runtime.run_turn(WorkerTurnRequest(
            prompt="do work",
            cwd=self.root,
            provider="codex",
            session_id="",
            timeout_seconds=5,
        ))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.events[0]["failure_kind"], "malformed_output")
        self.assertIn("malformed provider output", result.output_text)

    def test_local_cli_rejects_unsupported_budget_environment(self) -> None:
        runtime = LocalCliRuntime(executables={
            "codex": [sys.executable, str(self.fake_provider), "codex"],
        })
        result = runtime.run_turn(WorkerTurnRequest(
            prompt="do work",
            cwd=self.root,
            provider="codex",
            session_id="",
            timeout_seconds=5,
            environment={"MAX_BUDGET_USD": "1"},
        ))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.events[0]["failure_kind"], "unsupported_budget")

    def test_local_cli_parses_claude_stream_and_codex_json(self) -> None:
        cases = {
            "claude": "claude-stream",
            "codex": "codex",
            "gemini": "gemini",
        }
        for provider, mode in cases.items():
            with self.subTest(provider=provider):
                runtime = LocalCliRuntime(executables={
                    provider: [sys.executable, str(self.fake_provider), mode],
                }, **({
                    "claude_allowed_tools": ["Read"],
                    "claude_max_budget_usd": 1,
                } if provider == "claude" else {}))
                result = runtime.run_turn(WorkerTurnRequest(
                    prompt="do work",
                    cwd=self.root,
                    provider=provider,
                    session_id="",
                    timeout_seconds=5,
                ))
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.output_text, f"{provider} completed")
                self.assertEqual(result.session_id, f"{provider}-session")
                self.assertGreaterEqual(len(result.events), 1)

    def test_claude_permission_mode_is_passed_as_cli_flag(self) -> None:
        runner = _RecordingProcessRunner()
        runtime = LocalCliRuntime(
            executables={"claude": ["claude"]},
            process_runner=runner,
            claude_allowed_tools=["Read", "Edit"],
            claude_max_budget_usd=1,
            claude_permission_mode="acceptEdits",
        )
        result = runtime.run_turn(WorkerTurnRequest(
            prompt="do work",
            cwd=self.root,
            provider="claude",
            session_id="",
            timeout_seconds=5,
        ))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            runner.argv,
            [
                "claude", "-p", "--output-format", "stream-json", "--verbose",
                "--allowedTools", "Read,Edit", "--max-budget-usd", "1",
                "--permission-mode", "acceptEdits",
            ],
        )

    def test_provider_argv_contracts_pass_the_prompt_exactly_once(self) -> None:
        cases = (
            (
                "codex",
                {"codex": ["codex"]},
                {},
                ["codex", "exec", "--json", "-"],
                "do work",
                json.dumps({"session_id": "codex-session", "output_text": "{}"}) + "\n",
            ),
            (
                "gemini",
                {"gemini": ["gemini"]},
                {},
                ["gemini", "-p", "do work", "--output-format", "json"],
                None,
                json.dumps({"session_id": "gemini-session", "response": "{}"}),
            ),
            (
                "claude",
                {"claude": ["claude"]},
                {
                    "claude_allowed_tools": ["Bash", "Read", "Edit", "Write"],
                    "claude_max_budget_usd": 2.5,
                    "claude_permission_mode": "acceptEdits",
                },
                [
                    "claude", "-p", "--output-format", "stream-json", "--verbose",
                    "--allowedTools", "Bash,Read,Edit,Write", "--max-budget-usd", "2.5",
                    "--permission-mode", "acceptEdits",
                ],
                "do work",
                json.dumps({"type": "result", "session_id": "claude-session", "result": "{}"}) + "\n",
            ),
        )
        for provider, executables, settings, expected_argv, expected_input, stdout in cases:
            with self.subTest(provider=provider):
                runner = _ContractRecordingRunner(stdout)
                try:
                    runtime = LocalCliRuntime(
                        executables=executables,
                        process_runner=runner,
                        **settings,
                    )
                except TypeError as exc:
                    self.fail(f"provider controls are not supported: {exc}")
                result = runtime.run_turn(WorkerTurnRequest(
                    prompt="do work",
                    cwd=self.root,
                    provider=provider,
                    session_id="",
                    timeout_seconds=5,
                ))
                self.assertEqual(runner.argv, expected_argv)
                self.assertEqual(runner.input_text, expected_input)
                self.assertEqual(result.submission, {})

    def test_claude_stream_prefers_terminal_result_over_duplicate_assistant_text(self) -> None:
        stdout = "\n".join((
            json.dumps({
                "type": "assistant",
                "session_id": "claude-session",
                "message": {"content": [{"type": "text", "text": "{}"}]},
            }),
            json.dumps({
                "type": "result",
                "session_id": "claude-session",
                "result": "{}",
            }),
        )) + "\n"
        runtime = LocalCliRuntime(
            executables={"claude": ["claude"]},
            process_runner=_ContractRecordingRunner(stdout),
            claude_allowed_tools=["Read"],
            claude_max_budget_usd=1,
        )

        result = runtime.run_turn(WorkerTurnRequest(
            prompt="do work",
            cwd=self.root,
            provider="claude",
            session_id="",
            timeout_seconds=5,
        ))

        self.assertEqual(result.output_text, "{}")
        self.assertEqual(result.submission, {})

    def test_claude_rejects_missing_allowed_tools_or_budget_before_spawn(self) -> None:
        for settings, expected in (
            ({"claude_max_budget_usd": 2.5}, "allowed tools"),
            ({"claude_allowed_tools": ["Read"]}, "max budget"),
        ):
            with self.subTest(settings=settings):
                runner = _ContractRecordingRunner("{}")
                try:
                    runtime = LocalCliRuntime(
                        executables={"claude": ["claude"]},
                        process_runner=runner,
                        **settings,
                    )
                except TypeError as exc:
                    self.fail(f"Claude controls are not supported: {exc}")
                result = runtime.run_turn(WorkerTurnRequest(
                    prompt="do work", cwd=self.root, provider="claude",
                    session_id="", timeout_seconds=5,
                ))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.output_text)
                self.assertEqual(runner.argv, [])

    def test_runtime_config_selects_local_gemini_and_cao_without_using_host_route(self) -> None:
        try:
            runtime_config = importlib.import_module("twin.runtime.config")
        except ModuleNotFoundError:
            self.fail("twin.runtime.config is missing")
        local_path = self.root / "local.toml"
        local_path.write_text(
            '[runtime]\nadapter = "local_cli"\nworker_provider = "gemini"\ntimeout_seconds = 45\n',
            encoding="utf-8",
        )
        local = runtime_config.load_runtime_config(local_path)
        self.assertEqual(local.adapter, "local_cli")
        self.assertEqual(local.worker_provider, "gemini")
        self.assertEqual(local.timeout_seconds, 45)

        cao_path = self.root / "cao.toml"
        cao_path.write_text(
            '[runtime]\nadapter = "cao"\nworker_provider = "codex"\n'
            '[cao]\nendpoint = "http://127.0.0.1:7777/turn"\nauth_token_env = "TWIN_CAO_TOKEN"\n'
            'provider = "codex"\nagent = "worker"\n',
            encoding="utf-8",
        )
        cao = runtime_config.load_runtime_config(cao_path)
        selected = runtime_config.build_runtime(cao, {"TWIN_CAO_TOKEN": "secret"})
        self.assertIsInstance(selected, CaoRuntime)

    def test_pinned_real_cli_help_satisfies_the_provider_contract_probe(self) -> None:
        try:
            runtime_config = importlib.import_module("twin.runtime.config")
        except ModuleNotFoundError:
            self.fail("twin.runtime.config is missing")
        fixture_root = Path(__file__).parent / "fixtures" / "provider_help"
        versions = {
            "claude": "2.1.246",
            "codex": "0.149.1",
            "gemini": "0.57.0",
        }
        for provider, version in versions.items():
            with self.subTest(provider=provider):
                transcript = (fixture_root / f"{provider}-{version}.txt").read_text(encoding="utf-8")
                self.assertEqual(
                    runtime_config.validate_provider_help(provider, transcript), []
                )

    def test_provider_contract_gate_accepts_the_pinned_fixtures(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "check-provider-contracts.py"
        result = subprocess.run(
            [sys.executable, str(script)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_process_timeout_kills_child_process_group(self) -> None:
        marker = self.root / "child-survived.txt"
        child = (
            "import pathlib, sys, time; "
            "time.sleep(0.4); pathlib.Path(sys.argv[1]).write_text('alive', encoding='utf-8')"
        )
        parent = (
            "import subprocess, sys, time; "
            "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]]); "
            "time.sleep(5)"
        )
        result = ProcessRunner().run(
            [sys.executable, "-c", parent, child, str(marker)],
            cwd=self.root,
            environment={},
            timeout_seconds=0.1,
        )
        time.sleep(0.7)
        self.assertTrue(result.timed_out)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())

    def test_cao_rejects_plaintext_non_loopback_before_request(self) -> None:
        runtime = CaoRuntime("http://example.com/turn", auth_token="token", provider="codex", agent="worker")
        result = runtime.run_turn(WorkerTurnRequest(
            prompt="do work",
            cwd=self.root,
            provider="codex",
            session_id="",
            timeout_seconds=5,
        ))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.events[0]["failure_kind"], "cao_plaintext_non_loopback")

    def test_cao_requires_auth_token(self) -> None:
        runtime = CaoRuntime("http://127.0.0.1:9/turn", auth_token=None, provider="codex", agent="worker")
        result = runtime.run_turn(WorkerTurnRequest(
            prompt="do work",
            cwd=self.root,
            provider="codex",
            session_id="",
            timeout_seconds=5,
        ))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.events[0]["failure_kind"], "cao_auth_required")

    def test_cao_sends_auth_to_loopback_and_blocks_redirects(self) -> None:
        server = _CaoTestServer()
        self.addCleanup(server.close)
        ok = CaoRuntime(server.url("/ok"), auth_token="token", provider="codex", agent="worker").run_turn(
            WorkerTurnRequest("do work", self.root, "codex", "", 5)
        )
        self.assertEqual(ok.returncode, 0)
        self.assertEqual(ok.output_text, "cao completed")
        self.assertEqual(server.last_authorization, "Bearer token")

        redirected = CaoRuntime(server.url("/redirect"), auth_token="token", provider="codex", agent="worker").run_turn(
            WorkerTurnRequest("do work", self.root, "codex", "", 5)
        )
        self.assertNotEqual(redirected.returncode, 0)
        self.assertEqual(redirected.events[0]["failure_kind"], "cao_redirect_blocked")

        unauthorized = CaoRuntime(server.url("/auth"), auth_token="bad", provider="codex", agent="worker").run_turn(
            WorkerTurnRequest("do work", self.root, "codex", "", 5)
        )
        self.assertNotEqual(unauthorized.returncode, 0)
        self.assertEqual(unauthorized.events[0]["failure_kind"], "cao_auth_failed")

    def test_cao_requests_a_fresh_teardown_turn(self) -> None:
        server = _CaoTestServer()
        self.addCleanup(server.close)

        result = CaoRuntime(
            server.url("/ok"),
            auth_token="token",
            provider="codex",
            agent="worker",
        ).run_turn(WorkerTurnRequest("do work", self.root, "codex", "", 5))

        self.assertEqual(result.returncode, 0)
        assert server.last_body is not None
        self.assertIs(server.last_body.get("teardown"), True)

    def test_cao_parses_structured_worker_submission_from_output_text(self) -> None:
        server = _CaoTestServer()
        self.addCleanup(server.close)

        result = CaoRuntime(
            server.url("/submission"),
            auth_token="token",
            provider="codex",
            agent="worker",
        ).run_turn(WorkerTurnRequest("do work", self.root, "codex", "", 5))

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.submission, {
            "updates": [],
            "command_results": [],
            "artifacts": [],
        })

    def test_cao_rejects_boolean_returncode(self) -> None:
        server = _CaoTestServer()
        self.addCleanup(server.close)

        result = CaoRuntime(
            server.url("/boolean-returncode"),
            auth_token="token",
            provider="codex",
            agent="worker",
        ).run_turn(WorkerTurnRequest("do work", self.root, "codex", "", 5))

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.events[0].get("failure_kind"), "malformed_output")

    def test_cao_timeout_is_classified_as_timed_out(self) -> None:
        server = _CaoTestServer()
        self.addCleanup(server.close)
        result = CaoRuntime(server.url("/slow"), auth_token="token", provider="codex", agent="worker").run_turn(
            WorkerTurnRequest("do work", self.root, "codex", "", 0.05)
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.events[0]["failure_kind"], "timeout")


class WorktreeIsolationTest(TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.isolation = GitWorkspaceIsolation()

    def test_prepare_creates_branch_from_head_and_initializes_submodule(self) -> None:
        submodule_source = self.root / "lib-source"
        init_repo(submodule_source)
        parent = self.root / "repo"
        init_repo(parent)
        subprocess.run(
            [
                "git", "-C", str(parent), "-c", "protocol.file.allow=always",
                "submodule", "add", str(submodule_source), "modules/lib",
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        git(parent, "commit", "-am", "add submodule")
        head = git(parent, "rev-parse", "HEAD").stdout.strip()

        worktree = self.isolation.prepare(parent, "ws-1")

        self.assertEqual(git(worktree, "branch", "--show-current").stdout.strip(), "twin/ws-1")
        self.assertEqual(git(worktree, "rev-parse", "HEAD").stdout.strip(), head)
        self.assertTrue((worktree / "modules" / "lib" / "README.md").is_file())
        (worktree / "modules" / "lib" / "dirty.txt").write_text("dirty", encoding="utf-8")
        self.assertFalse(self.isolation.cleanup(parent, "ws-1"))
        self.assertTrue(worktree.exists())
        (worktree / "modules" / "lib" / "dirty.txt").unlink()
        self.assertTrue(self.isolation.cleanup(parent, "ws-1"))
        self.assertFalse(worktree.exists())

    def test_prepare_rejects_existing_checkout_on_wrong_branch(self) -> None:
        repo = self.root / "repo"
        init_repo(repo)
        existing = self.root / "repo-twin-ws-1"
        git(repo, "worktree", "add", "-b", "wrong-branch", str(existing), "HEAD")

        with self.assertRaisesRegex(RuntimeError, "branch mismatch"):
            self.isolation.prepare(repo, "ws-1")

    def test_cleanup_preserves_dirty_worktree(self) -> None:
        repo = self.root / "repo"
        init_repo(repo)
        worktree = self.isolation.prepare(repo, "ws-1")
        (worktree / "unsaved.txt").write_text("keep", encoding="utf-8")
        self.assertFalse(self.isolation.cleanup(repo, "ws-1"))
        self.assertTrue(worktree.exists())

    def test_cleanup_preserves_clean_worktree_with_unintegrated_commit(self) -> None:
        repo = self.root / "repo"
        init_repo(repo)
        worktree = self.isolation.prepare(repo, "ws-1")
        (worktree / "feature.txt").write_text("worker output\n", encoding="utf-8")
        git(worktree, "add", "feature.txt")
        git(worktree, "commit", "-m", "worker output")

        self.assertFalse(self.isolation.cleanup(repo, "ws-1"))

        self.assertTrue(worktree.exists())
        self.assertEqual(git(worktree, "branch", "--show-current").stdout.strip(), "twin/ws-1")
        self.assertEqual(git(repo, "rev-parse", "--verify", "twin/ws-1").returncode, 0)

    def test_prepare_uses_full_workspace_id_not_only_basename(self) -> None:
        repo = self.root / "repo"
        init_repo(repo)
        first = self.isolation.prepare(repo, "alpha/ws")
        second = self.isolation.prepare(repo, "beta/ws")
        self.assertNotEqual(first, second)
        self.assertEqual(git(first, "branch", "--show-current").stdout.strip(), "twin/alpha-ws")
        self.assertEqual(git(second, "branch", "--show-current").stdout.strip(), "twin/beta-ws")

    def test_prepare_reports_missing_git(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        missing = self.root / "missing-git"
        with self.assertRaisesRegex(RuntimeError, "git executable not found"):
            GitWorkspaceIsolation(git_binary=str(missing)).prepare(repo, "ws-1")


class TwinServiceRuntimeIntegrationTest(TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        self.store = WorkspaceStore(TwinPaths.for_home(root / "home"))
        self.runtime = _DeterministicRuntime()
        self.isolation = _DeterministicIsolation()
        self.resources = ResourceCatalog(Path(__file__).resolve().parents[1])
        self.service = TwinService(
            self.store, runtime=self.runtime, isolation=self.isolation, resources=self.resources
        )

    def test_run_applies_worker_submission_before_review_and_can_reach_accepted_done(self) -> None:
        start = self.service.start("ship feature", self.repo, "host/codex")
        ready = self.service.submit_plan(
            start["workspace"], "host/codex", start["state_revision"], start["action_token"],
            valid_goal_and_plan(),
        )

        review = self.service.run(ready["workspace"], self.repo, "host/codex")

        self.assertEqual(review["action"], "review")
        workspace = self.store.resolve(str(review["workspace"]), self.repo)
        state = self.store.load_state(workspace)
        run_id = review["context"]["run"]["run_id"]
        self.assertEqual(state["status"], "review_required")
        self.assertNotEqual(state["status"], "accepted_done")
        self.assertEqual(self.runtime.requests[0].cwd, self.repo.resolve() / ".isolated")
        request_payload = json.loads((workspace / "runs" / run_id / "request.json").read_text(encoding="utf-8"))
        result_payload = json.loads((workspace / "runs" / run_id / "result.json").read_text(encoding="utf-8"))
        evidence_payload = json.loads((workspace / "runs" / run_id / "evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(request_payload["environment"], {})
        self.assertNotIn("action_token", request_payload["prompt"])
        self.assertEqual(result_payload["output_text"], "deterministic fake output")
        plan = load_yaml(workspace / "plan.yaml")
        self.assertEqual(plan["items"][0]["status"], "completed")
        self.assertEqual(plan["items"][0]["actual_evidence"], ["artifacts/evidence.txt"])
        self.assertEqual((workspace / "artifacts" / "evidence.txt").read_text(encoding="utf-8"), "verified")
        self.assertEqual(
            validate_document(evidence_payload, "run-evidence", self.service.resources),
            [],
        )
        self.assertEqual(evidence_payload["request"]["relative"], f"runs/{run_id}/request.json")
        self.assertEqual(evidence_payload["result"]["relative"], f"runs/{run_id}/result.json")
        event_stream = (workspace / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("runs/" + run_id + "/result.json", event_stream)
        self.assertNotIn("deterministic fake output", event_stream)

        accepted = self.service.submit_review(
            review["workspace"], "host/codex", review["state_revision"],
            review["action_token"], run_id, {"decision": "accepted"},
        )
        self.assertEqual(accepted["status"], "accepted_done")

    def test_worker_prompt_materializes_run_bound_command_evidence(self) -> None:
        runtime = _PromptBoundCommandRuntime()
        service = TwinService(self.store, runtime=runtime, resources=self.resources)
        start = service.start("ship feature", self.repo, "host/codex")
        payload = valid_goal_and_plan()
        goal = payload["goal"]
        plan = payload["plan"]
        assert isinstance(goal, dict) and isinstance(plan, dict)
        criteria = goal["acceptance_criteria"]
        items = plan["items"]
        assert isinstance(criteria, list) and isinstance(items, list)
        assert isinstance(criteria[0], dict) and isinstance(items[0], dict)
        criteria[0]["evidence_type"] = "command"
        items[0]["evidence_plan"] = [
            "command:artifacts/runs/{run_id}/tests.json"
        ]
        ready = service.submit_plan(
            start["workspace"], "host/codex", start["state_revision"],
            start["action_token"], payload,
        )

        review = service.run(ready["workspace"], self.repo, "host/codex")

        run_id = str(review["context"]["run"]["run_id"])
        evidence = f"command:artifacts/runs/{run_id}/tests.json"
        self.assertEqual(review["context"]["run"]["status"], "completed")
        self.assertIn(f'"run_id": "{run_id}"', runtime.prompt)
        self.assertIn(evidence, runtime.prompt)
        workspace = self.store.resolve(str(review["workspace"]), self.repo)
        persisted_plan = load_yaml(workspace / "plan.yaml")
        self.assertEqual(persisted_plan["items"][0]["evidence_plan"], [evidence])
        self.assertEqual(persisted_plan["items"][0]["actual_evidence"], [evidence])
        accepted = service.submit_review(
            review["workspace"], "host/codex", review["state_revision"],
            review["action_token"], run_id, {"decision": "accepted"},
        )
        self.assertEqual(accepted["status"], "accepted_done")

    def test_review_and_status_reject_modified_evidence_content(self) -> None:
        start = self.service.start("ship feature", self.repo, "host/codex")
        ready = self.service.submit_plan(
            start["workspace"], "host/codex", start["state_revision"], start["action_token"],
            valid_goal_and_plan(),
        )
        review = self.service.run(ready["workspace"], self.repo, "host/codex")
        workspace = self.store.resolve(str(review["workspace"]), self.repo)
        (workspace / "artifacts" / "evidence.txt").write_text("tampered", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "artifact integrity mismatch"):
            self.service.status(review["workspace"], self.repo)
        with self.assertRaisesRegex(ValueError, "artifact integrity mismatch"):
            self.service.submit_review(
                review["workspace"], "host/codex", review["state_revision"],
                review["action_token"], review["context"]["run"]["run_id"],
                {"decision": "accepted"},
            )

    def test_run_recovers_the_same_worker_run_after_process_death(self) -> None:
        start = self.service.start("ship feature", self.repo, "host/codex")
        ready = self.service.submit_plan(
            start["workspace"], "host/codex", start["state_revision"], start["action_token"],
            valid_goal_and_plan(),
        )
        crashing = TwinService(
            self.store,
            runtime=_CrashingRuntime(),
            isolation=self.isolation,
            resources=self.resources,
        )

        with self.assertRaises(SystemExit):
            crashing.run(ready["workspace"], self.repo, "host/codex")

        workspace = self.store.resolve(str(ready["workspace"]), self.repo)
        stranded = self.store.load_state(workspace)
        run_id = stranded["current_run_id"]
        self.assertEqual(stranded["status"], "worker_running")
        request_path = workspace / "runs" / str(run_id) / "request.json"
        self.assertTrue(request_path.is_file())
        request_payload = json.loads(request_path.read_text(encoding="utf-8"))
        self.assertNotIn("action_token", request_payload["prompt"])

        recovered = self.service.run(ready["workspace"], self.repo, "host/codex")

        self.assertEqual(recovered["action"], "review")
        self.assertEqual(recovered["context"]["run"]["run_id"], run_id)
        self.assertEqual(self.store.load_state(workspace)["status"], "review_required")

    def test_run_recovery_rejects_provider_contract_version_mismatch(self) -> None:
        start = self.service.start("ship feature", self.repo, "host/codex")
        ready = self.service.submit_plan(
            start["workspace"], "host/codex", start["state_revision"], start["action_token"],
            valid_goal_and_plan(),
        )
        crashing = TwinService(
            self.store,
            runtime=_CrashingRuntime(),
            isolation=self.isolation,
            resources=self.resources,
            provider_contract_version=1,
        )

        with self.assertRaises(SystemExit):
            crashing.run(ready["workspace"], self.repo, "host/codex")

        incompatible = TwinService(
            self.store,
            runtime=self.runtime,
            isolation=self.isolation,
            resources=self.resources,
            provider_contract_version=2,
        )
        with self.assertRaisesRegex(ValueError, "worker runtime configuration mismatch"):
            incompatible.run(ready["workspace"], self.repo, "host/codex")

    def test_concurrent_run_rejects_duplicate_worker_execution(self) -> None:
        runtime = _BlockingRuntime()
        service = TwinService(
            self.store,
            runtime=runtime,
            isolation=self.isolation,
            resources=self.resources,
        )
        start = service.start("ship feature", self.repo, "host/codex")
        ready = service.submit_plan(
            start["workspace"], "host/codex", start["state_revision"],
            start["action_token"], valid_goal_and_plan(),
        )
        first_result: list[dict[str, object]] = []
        first_errors: list[BaseException] = []

        def run_first() -> None:
            try:
                first_result.append(
                    service.run(ready["workspace"], self.repo, "host/codex")
                )
            except BaseException as exc:
                first_errors.append(exc)

        first = Thread(target=run_first)
        first.start()
        self.assertTrue(runtime.started.wait(timeout=5))
        try:
            with self.assertRaisesRegex(RuntimeError, "workspace is busy"):
                service.run(ready["workspace"], self.repo, "host/codex")
        finally:
            runtime.release.set()
            first.join(timeout=5)

        self.assertFalse(first.is_alive())
        self.assertEqual(first_errors, [])
        self.assertEqual(len(first_result), 1)
        self.assertEqual(first_result[0]["action"], "review")
        self.assertEqual(len(runtime.requests), 1)

    def test_final_publish_failure_leaves_only_resumable_worker_state(self) -> None:
        start = self.service.start("ship feature", self.repo, "host/codex")
        ready = self.service.submit_plan(
            start["workspace"], "host/codex", start["state_revision"], start["action_token"],
            valid_goal_and_plan(),
        )
        original = self.store.commit_action

        def fail_final_publish(*args: object, **kwargs: object) -> None:
            event = kwargs.get("event")
            if isinstance(event, dict) and event.get("event") == "worker_completed":
                raise SystemExit("simulated process death before final publication")
            original(*args, **kwargs)

        with patch.object(self.store, "commit_action", side_effect=fail_final_publish):
            with self.assertRaises(SystemExit):
                self.service.run(ready["workspace"], self.repo, "host/codex")

        workspace = self.store.resolve(str(ready["workspace"]), self.repo)
        state = self.store.load_state(workspace)
        run_root = workspace / "runs" / str(state["current_run_id"])
        self.assertEqual(state["status"], "worker_running")
        self.assertTrue((run_root / "request.json").is_file())
        self.assertFalse((run_root / "result.json").exists())
        self.assertFalse((run_root / "evidence.json").exists())

        recovered = self.service.run(ready["workspace"], self.repo, "host/codex")
        self.assertEqual(recovered["action"], "review")

    def test_prepare_failure_persists_failed_evidence_and_review_required_state(self) -> None:
        service = TwinService(
            self.store,
            runtime=self.runtime,
            isolation=_FailingIsolation(),
            resources=self.resources,
        )
        start = service.start("ship feature", self.repo, "host/codex")
        ready = service.submit_plan(
            start["workspace"], "host/codex", start["state_revision"], start["action_token"],
            valid_goal_and_plan(),
        )

        review = service.run(ready["workspace"], self.repo, "host/codex")

        workspace = self.store.resolve(str(review["workspace"]), self.repo)
        run_id = review["context"]["run"]["run_id"]
        state = self.store.load_state(workspace)
        result_payload = json.loads((workspace / "runs" / run_id / "result.json").read_text(encoding="utf-8"))
        evidence_payload = json.loads((workspace / "runs" / run_id / "evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(review["action"], "review")
        self.assertEqual(state["status"], "review_required")
        self.assertEqual(result_payload["events"][0]["failure_kind"], "isolation_prepare_failed")
        self.assertEqual(evidence_payload["status"], "failed")
        self.assertEqual(self.runtime.requests, [])

    def test_worker_submission_rejects_duplicate_artifact_paths(self) -> None:
        for collision in ("command", "cross-kind"):
            with self.subTest(collision=collision):
                service = TwinService(
                    self.store,
                    runtime=_DuplicateArtifactRuntime(collision),
                    resources=self.resources,
                )
                start = service.start("ship feature", self.repo, "host/codex")
                ready = service.submit_plan(
                    start["workspace"], "host/codex", start["state_revision"],
                    start["action_token"], valid_goal_and_plan(),
                )

                review = service.run(ready["workspace"], self.repo, "host/codex")

                self.assertEqual(review["context"]["run"]["status"], "failed")
                workspace = self.store.resolve(str(review["workspace"]), self.repo)
                run_id = str(review["context"]["run"]["run_id"])
                result = json.loads(
                    (workspace / "runs" / run_id / "result.json").read_text(encoding="utf-8")
                )
                self.assertEqual(result["events"][-1]["failure_kind"], "invalid_submission")
                self.assertFalse(
                    (workspace / "artifacts" / "runs" / run_id / "collision.json").exists()
                )


class _CaoTestHandler(BaseHTTPRequestHandler):
    server: "_CaoHttpServer"

    def do_POST(self) -> None:
        self.server.last_authorization = self.headers.get("Authorization")
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", self.server.url("/ok"))
            self.end_headers()
            return
        if self.path == "/slow":
            time.sleep(0.2)
        if self.path == "/auth" and self.headers.get("Authorization") != "Bearer token":
            self.send_response(401)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        self.server.last_body = body
        response = {
            "output_text": (
                json.dumps({
                    "updates": [],
                    "command_results": [],
                    "artifacts": [],
                })
                if self.path == "/submission"
                else "cao completed"
            ),
            "returncode": True if self.path == "/boolean-returncode" else 0,
            "session_id": f"{body['provider']}-{body['agent']}",
            "events": [{"event": "completed"}],
        }
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except BrokenPipeError:
            return

    def log_message(self, format: str, *args: Any) -> None:
        return


class _CaoHttpServer(ThreadingHTTPServer):
    last_authorization: str | None = None
    last_body: dict[str, object] | None = None

    def url(self, path: str) -> str:
        host, port = self.server_address
        return f"http://{host}:{port}{path}"


class _CaoTestServer:
    def __init__(self) -> None:
        self.server = _CaoHttpServer(("127.0.0.1", 0), _CaoTestHandler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def last_authorization(self) -> str | None:
        return self.server.last_authorization

    @property
    def last_body(self) -> dict[str, object] | None:
        return self.server.last_body

    def url(self, path: str) -> str:
        return self.server.url(path)

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


class _DeterministicRuntime:
    def __init__(self) -> None:
        self.requests: list[WorkerTurnRequest] = []

    def run_turn(self, request: WorkerTurnRequest) -> WorkerTurnResult:
        self.requests.append(request)
        return WorkerTurnResult(
            output_text="deterministic fake output",
            returncode=0,
            session_id="fake-session",
            events=({"event": "fake", "summary": "completed"},),
            submission={
                "updates": [{
                    "item_id": "implement",
                    "status": "completed",
                    "actual_evidence": ["artifacts/evidence.txt"],
                }],
                "command_results": [],
                "artifacts": [{
                    "relative": "artifacts/evidence.txt",
                    "content": "verified",
                }],
            },
        )


class _PromptBoundCommandRuntime:
    prompt = ""

    def run_turn(self, request: WorkerTurnRequest) -> WorkerTurnResult:
        self.prompt = request.prompt
        match = re.search(
            r"command:artifacts/runs/(run-[0-9a-f]+)/tests\.json",
            request.prompt,
        )
        relative = (
            f"artifacts/runs/{match.group(1)}/tests.json"
            if match is not None
            else "artifacts/runs/{run_id}/tests.json"
        )
        return WorkerTurnResult(
            output_text="command evidence",
            returncode=0,
            session_id="command-evidence",
            events=({"event": "completed"},),
            submission={
                "updates": [{
                    "item_id": "implement",
                    "status": "completed",
                    "actual_evidence": [f"command:{relative}"],
                }],
                "command_results": [{
                    "relative": relative,
                    "exit_code": 0,
                    "argv": ["python3", "-m", "unittest"],
                    "stdout": "OK",
                    "stderr": "",
                }],
                "artifacts": [],
            },
        )


class _CrashingRuntime:
    def run_turn(self, request: WorkerTurnRequest) -> WorkerTurnResult:
        del request
        raise SystemExit("simulated process death")


class _BlockingRuntime(_DeterministicRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()
        self.calls = 0

    def run_turn(self, request: WorkerTurnRequest) -> WorkerTurnResult:
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            if not self.release.wait(timeout=5):
                raise RuntimeError("test worker was not released")
        return super().run_turn(request)


class _DuplicateArtifactRuntime:
    def __init__(self, collision: str) -> None:
        self.collision = collision

    def run_turn(self, request: WorkerTurnRequest) -> WorkerTurnResult:
        match = re.search(r'"run_id": "(run-[0-9a-f]+)"', request.prompt)
        assert match is not None
        relative = f"artifacts/runs/{match.group(1)}/collision.json"
        command_results = [{"relative": relative, "exit_code": 0}]
        artifacts = [{"relative": "artifacts/evidence.txt", "content": "verified"}]
        if self.collision == "command":
            command_results.append({"relative": relative, "exit_code": 1})
        else:
            artifacts.append({"relative": relative, "content": "forged"})
        return WorkerTurnResult(
            output_text="duplicate artifact",
            returncode=0,
            session_id="duplicate-artifact",
            events=({"event": "completed"},),
            submission={
                "updates": [{
                    "item_id": "implement",
                    "status": "completed",
                    "actual_evidence": ["artifacts/evidence.txt"],
                }],
                "command_results": command_results,
                "artifacts": artifacts,
            },
        )


class _DeterministicIsolation:
    def __init__(self) -> None:
        self.cleaned: list[tuple[Path, str]] = []

    def prepare(self, repo_root: Path, workspace_id: str) -> Path:
        return repo_root / ".isolated"

    def cleanup(self, repo_root: Path, workspace_id: str) -> bool:
        self.cleaned.append((repo_root, workspace_id))
        return True


class _FailingIsolation:
    def prepare(self, repo_root: Path, workspace_id: str) -> Path:
        del repo_root, workspace_id
        raise RuntimeError("prepare exploded")

    def cleanup(self, repo_root: Path, workspace_id: str) -> bool:
        del repo_root, workspace_id
        raise AssertionError("cleanup must not run when prepare failed")


class _RecordingProcessRunner:
    def __init__(self) -> None:
        self.argv: list[str] = []

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: float,
        input_text: str | None = None,
    ) -> ProcessResult:
        del cwd, environment, timeout_seconds, input_text
        self.argv = list(argv)
        return ProcessResult(
            stdout=json.dumps({
                "type": "assistant",
                "session_id": "claude-session",
                "message": {"content": [{"type": "text", "text": "claude completed"}]},
            }) + "\n",
            stderr="",
            returncode=0,
            timed_out=False,
        )


class _StaticProcessRunner:
    def __init__(self, *, stdout: str, returncode: int) -> None:
        self.stdout = stdout
        self.returncode = returncode

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: float,
        input_text: str | None = None,
    ) -> ProcessResult:
        del argv, cwd, environment, timeout_seconds, input_text
        return ProcessResult(
            stdout=self.stdout,
            stderr="",
            returncode=self.returncode,
            timed_out=False,
        )


class _ContractRecordingRunner:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.argv: list[str] = []
        self.input_text: str | None = None

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: float,
        input_text: str | None = None,
    ) -> ProcessResult:
        del cwd, environment, timeout_seconds
        self.argv = list(argv)
        self.input_text = input_text
        return ProcessResult(
            stdout=self.stdout,
            stderr="",
            returncode=0,
            timed_out=False,
        )
