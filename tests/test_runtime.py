from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from tempfile import TemporaryDirectory
from threading import Thread
from typing import Any
from unittest import TestCase
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from twin.domain.service import TwinService
from twin.paths import TwinPaths
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

    def test_worker_request_has_no_dev_rules_environment(self) -> None:
        request = WorkerTurnRequest(
            prompt="do work",
            cwd=Path("/tmp/repo"),
            provider="codex",
            session_id="",
            timeout_seconds=30,
            environment={"DEV_RULES": "/tmp/dev-rules", "KEEP": "1"},
        )
        self.assertNotIn("DEV_RULES", request.environment)
        self.assertEqual(request.environment["KEEP"], "1")

    def test_local_cli_removes_dev_rules_from_process_environment(self) -> None:
        runtime = LocalCliRuntime(executables={
            "codex": [sys.executable, str(self.fake_provider), "env"],
        })
        request = WorkerTurnRequest(
            prompt="do work",
            cwd=self.root,
            provider="codex",
            session_id="session-in",
            timeout_seconds=5,
            environment={"DEV_RULES": "/tmp/dev-rules", "VISIBLE": "yes"},
        )
        with self.subTest("host environment is scrubbed"):
            old_dev_rules = os.environ.get("DEV_RULES")
            os.environ["DEV_RULES"] = "/tmp/host-dev-rules"
            try:
                result = runtime.run_turn(request)
            finally:
                if old_dev_rules is None:
                    os.environ.pop("DEV_RULES", None)
                else:
                    os.environ["DEV_RULES"] = old_dev_rules
        payload = json.loads(result.output_text)
        self.assertEqual(payload, {"DEV_RULES": None, "VISIBLE": "yes"})

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
                })
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
            executables={"claude_headless": ["claude"]},
            process_runner=runner,
        )
        result = runtime.run_turn(WorkerTurnRequest(
            prompt="do work",
            cwd=self.root,
            provider="claude_headless",
            session_id="",
            timeout_seconds=5,
            environment={"TWIN_PERMISSION_MODE": "acceptEdits"},
        ))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            runner.argv,
            ["claude", "--permission-mode", "acceptEdits"],
        )

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
        self.service = TwinService(self.store, runtime=self.runtime, isolation=self.isolation)

    def test_run_persists_runtime_evidence_and_transitions_to_review_required(self) -> None:
        start = self.service.start("ship feature", self.repo, "host/codex")
        ready = self.service.submit_plan(
            start["workspace"], "host/codex", start["state_revision"], start["action_token"],
            valid_goal_and_plan(),
        )

        review = self.service.run(ready["workspace"], self.repo, "host/codex")

        self.assertEqual(review["action"], "review")
        workspace = self.store.resolve(str(review["workspace"]), self.repo)
        state = self.store.load_state(workspace)
        run_id = review["context"]["metadata"]["run_id"]
        self.assertEqual(state["status"], "review_required")
        self.assertNotEqual(state["status"], "accepted_done")
        self.assertEqual(self.runtime.requests[0].cwd, self.repo.resolve() / ".isolated")
        request_payload = json.loads((workspace / "runs" / run_id / "request.json").read_text(encoding="utf-8"))
        result_payload = json.loads((workspace / "runs" / run_id / "result.json").read_text(encoding="utf-8"))
        evidence_payload = json.loads((workspace / "runs" / run_id / "evidence.json").read_text(encoding="utf-8"))
        self.assertNotIn("DEV_RULES", request_payload["environment"])
        self.assertEqual(result_payload["output_text"], "deterministic fake output")
        self.assertEqual(
            validate_document(evidence_payload, "run-evidence", self.service.resources),
            [],
        )
        event_stream = (workspace / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("runs/" + run_id + "/result.json", event_stream)
        self.assertNotIn("deterministic fake output", event_stream)

    def test_prepare_failure_persists_failed_evidence_and_review_required_state(self) -> None:
        service = TwinService(
            self.store,
            runtime=self.runtime,
            isolation=_FailingIsolation(),
        )
        start = service.start("ship feature", self.repo, "host/codex")
        ready = service.submit_plan(
            start["workspace"], "host/codex", start["state_revision"], start["action_token"],
            valid_goal_and_plan(),
        )

        review = service.run(ready["workspace"], self.repo, "host/codex")

        workspace = self.store.resolve(str(review["workspace"]), self.repo)
        run_id = review["context"]["metadata"]["run_id"]
        state = self.store.load_state(workspace)
        result_payload = json.loads((workspace / "runs" / run_id / "result.json").read_text(encoding="utf-8"))
        evidence_payload = json.loads((workspace / "runs" / run_id / "evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(review["action"], "review")
        self.assertEqual(state["status"], "review_required")
        self.assertEqual(result_payload["events"][0]["failure_kind"], "isolation_prepare_failed")
        self.assertEqual(evidence_payload["status"], "failed")
        self.assertEqual(self.runtime.requests, [])


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
        response = {
            "output_text": "cao completed",
            "returncode": 0,
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
