"""Drive Twin's installed-wheel lifecycle without importing Twin source code."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional


def main() -> int:
    twin = Path(os.environ["TWIN_BIN"])
    if not twin.is_file() or not os.access(twin, os.X_OK):
        raise AssertionError(f"installed Twin console script is unavailable: {twin}")

    with tempfile.TemporaryDirectory(prefix="twin-smoke-") as raw:
        root = Path(raw)
        home = root / "home"
        repo = root / "target-repository"
        repo.mkdir()
        env = dict(os.environ)
        env["HOME"] = str(home)
        env["PATH"] = str(twin.parent) + os.pathsep + env.get("PATH", "")
        _install_fake_git(root, env)
        _install_fake_codex(root)

        _json_command(twin, env, "setup", "--json")
        config = home / ".twin" / "config.toml"
        config.write_text(
            '[runtime]\nadapter = "local_cli"\nworker_provider = "codex"\n'
            'timeout_seconds = 30\n',
            encoding="utf-8",
        )
        contract = _json_command(twin, env, "contract", "--json")
        _require(
            contract["action_commands"] == ["submit-plan", "submit-review"],
            "contract action commands are stale",
        )
        doctor = _json_command(twin, env, "doctor", "--json")
        _require(doctor["ok"], "installed Twin health gate failed")
        _require(
            doctor["checks"]["runtime_configuration"]["ok"],
            "runtime configuration is unhealthy",
        )

        started = _json_command(
            twin,
            env,
            "start",
            "ship the smoke lifecycle",
            "--supervisor",
            "host/antigravity",
            "--json",
            cwd=repo,
        )
        workspace = _string(started, "workspace")
        _require(
            _object(_object(started, "expected_output"), "payload")["required"]
            == ["goal", "plan"],
            "author action omitted its payload contract",
        )
        planned = _json_action_submission(
            env, started, _plan_payload(), cwd=repo
        )
        _require(planned["status"] == "ready", "plan submission did not become ready")
        replay = _command_argv(
            env,
            _action_argv(started, "submit"),
            cwd=repo,
            input_text=json.dumps(_plan_payload()),
        )
        _require(replay.returncode != 0, "replayed action token was accepted")
        _require(
            "stale or consumed action" in replay.stderr,
            f"token replay was rejected for the wrong reason: {replay.stderr!r}",
        )

        run_argv = _action_argv(planned, "next_command")
        crashed = _command_argv(env, run_argv, cwd=repo)
        workspace_root = home / ".twin" / "workspaces" / workspace
        failure_detail = ""
        if crashed.returncode == 0:
            failed_state = _json_file(workspace_root / "state.json")
            failed_run_id = failed_state.get("current_run_id")
            if isinstance(failed_run_id, str):
                failed_result = workspace_root / "runs" / failed_run_id / "result.json"
                if failed_result.is_file():
                    failure_detail = f" result={failed_result.read_text(encoding='utf-8')!r}"
        _require(
            crashed.returncode != 0,
            "first worker run did not kill the Twin process: "
            f"stdout={crashed.stdout!r} stderr={crashed.stderr!r}{failure_detail}",
        )

        stranded = _json_file(workspace_root / "state.json")
        _require(stranded["status"] == "worker_running", "crash did not leave resumable state")
        run_id = _string(stranded, "current_run_id")
        run_root = workspace_root / "runs" / run_id
        request_path = run_root / "request.json"
        request_text = request_path.read_text(encoding="utf-8")
        request_payload = json.loads(request_text)
        _require((run_root / "result.json").exists() is False, "crashed run published a result")
        _require((run_root / "evidence.json").exists() is False, "crashed run published evidence")
        _require(request_payload["run_id"] == run_id, "run request identity changed")
        _require(
            f"command:artifacts/runs/{run_id}/tests.json" in request_payload["prompt"],
            "run-bound command evidence was not materialized in the prompt",
        )
        _require("action_token" not in request_text, "run request retained an action token field")
        _require(
            _string(started, "action_token") not in request_text,
            "run request retained the author token",
        )

        reviewed = _json_argv(env, run_argv, cwd=repo)
        _require(reviewed["action"] == "review", "recovered run did not issue review")
        _require(reviewed["next_command"] is None, "review action invented a continuation")
        run_context = _object(_object(reviewed, "context"), "run")
        _require(run_context["run_id"] == run_id, "recovery created a different run")
        _require(run_context["status"] == "completed", "worker submission was not completed")

        result_payload = _json_file(run_root / "result.json")
        evidence_payload = _json_file(run_root / "evidence.json")
        _require(result_payload["returncode"] == 0, "worker runtime did not succeed")
        _require(evidence_payload["status"] == "completed", "worker evidence was not completed")
        plan_text = (workspace_root / "plan.yaml").read_text(encoding="utf-8")
        expected_evidence = f"command:artifacts/runs/{run_id}/tests.json"
        _require(expected_evidence in plan_text, "plan did not persist run-bound evidence")

        worktree = repo.parent / f"{repo.name}-twin-{workspace}"
        _require(worktree.is_dir(), "dirty worker worktree did not survive Twin run")
        _require((worktree / "dirty-preserve.txt").is_file(), "dirty worker file was not preserved")
        first_invocation = _json_file(root / "bin" / "codex-invocation-1.json")
        second_invocation = _json_file(root / "bin" / "codex-invocation-2.json")
        for invocation in (first_invocation, second_invocation):
            _require(
                invocation["argv"] == ["exec", "--json", "-"],
                "Codex did not receive the exact exec --json - argv",
            )
            _require(invocation["cwd"] == str(worktree), "Codex did not run in the worktree")
            _require(
                _string(started, "action_token") not in invocation["stdin"],
                "worker prompt retained the author token",
            )
        _require(
            first_invocation["stdin"] == second_invocation["stdin"],
            "recovery did not replay the same persisted request",
        )

        accepted = _json_action_submission(
            env, reviewed, {"decision": "accepted"}, cwd=repo
        )
        _require(accepted["status"] == "accepted_done", "review did not accept completion")
        _require(accepted["next_command"] is None, "terminal result did not stop the command chain")

        for path in (request_path, run_root / "result.json", run_root / "evidence.json"):
            text = path.read_text(encoding="utf-8")
            for token in (_string(started, "action_token"), _string(reviewed, "action_token")):
                _require(token not in text, f"run artifact retained an action token: {path.name}")

        setup_check = _json_command(twin, env, "setup", "--check", "--json")
        _require(setup_check["ok"], "setup check failed after lifecycle")
    return 0


def _plan_payload() -> dict[str, object]:
    return {
        "goal": {
            "schema_version": 1,
            "id": "assigned-by-service",
            "one_liner": "Ship the smoke lifecycle",
            "core_goal": "Verify the installed Twin lifecycle",
            "acceptance_criteria": [
                {"id": "ac-1", "statement": "Lifecycle completes", "evidence_type": "command"},
            ],
            "non_goals": [],
        },
        "plan": {
            "schema_version": 1,
            "goal_id": "assigned-by-service",
            "items": [
                {
                    "id": "verify",
                    "deliverable": "Installed lifecycle proof",
                    "scope": "Smoke fixture only",
                    "covers_ac": ["ac-1"],
                    "evidence_plan": ["command:artifacts/runs/{run_id}/tests.json"],
                    "actual_evidence": [],
                    "depends_on": [],
                    "status": "pending",
                    "next_action": "Run the lifecycle",
                },
            ],
            "verification": ["Run the isolated installed-wheel smoke"],
        },
    }


def _install_fake_git(root: Path, env: dict[str, str]) -> None:
    """Provide only the Git worktree contract the offline smoke needs."""
    bin_dir = root / "bin"
    bin_dir.mkdir()
    git = bin_dir / "git"
    git.write_text(
        """#!/opt/twin-venv/bin/python
import sys
from pathlib import Path


def main():
    args = sys.argv[1:]
    cwd = Path.cwd()
    if args[:1] == ["-C"]:
        cwd = Path(args[1])
        args = args[2:]
    if args[:2] == ["rev-parse", "--show-toplevel"]:
        print(cwd)
        return 0
    if args[:2] == ["rev-parse", "--git-common-dir"]:
        marker = cwd / ".fake-repo"
        repo = Path(marker.read_text(encoding="utf-8")) if marker.is_file() else cwd
        (repo / ".git").mkdir(exist_ok=True)
        print(repo / ".git")
        return 0
    if args[:3] == ["rev-parse", "--verify", "--quiet"]:
        return 1
    if args[:2] == ["rev-parse", "--verify"]:
        print("synthetic-head")
        return 0
    if args[:2] == ["worktree", "add"]:
        branch = args[3]
        path = Path(args[4])
        path.mkdir()
        (cwd / ".git").mkdir(exist_ok=True)
        (path / ".git").write_text("gitdir: synthetic\\n", encoding="utf-8")
        (path / ".fake-repo").write_text(str(cwd), encoding="utf-8")
        (path / ".fake-branch").write_text(branch, encoding="utf-8")
        (path / "dirty-preserve.txt").write_text("preserve\\n", encoding="utf-8")
        return 0
    if args[:2] == ["branch", "--show-current"]:
        print((cwd / ".fake-branch").read_text(encoding="utf-8").strip())
        return 0
    if args[:2] == ["status", "--porcelain=v1"]:
        if (cwd / "dirty-preserve.txt").is_file():
            print("?? dirty-preserve.txt")
        return 0
    if args[:2] == ["submodule", "status"]:
        return 0
    if args[:2] == ["-c", "protocol.file.allow=always"]:
        return 0
    return 0


raise SystemExit(main())
""",
        encoding="utf-8",
    )
    git.chmod(0o755)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")


def _install_fake_codex(root: Path) -> None:
    codex = root / "bin" / "codex"
    codex.write_text(
        """#!/opt/twin-venv/bin/python
import json
import os
import re
import signal
import sys
from pathlib import Path


args = sys.argv[1:]
prompt = sys.stdin.read()
counter_path = Path(__file__).with_name("codex-counter.txt")
count = int(counter_path.read_text(encoding="utf-8")) + 1 if counter_path.is_file() else 1
counter_path.write_text(str(count), encoding="utf-8")
record = Path(__file__).with_name(f"codex-invocation-{count}.json")
record.write_text(
    json.dumps({"argv": args, "cwd": str(Path.cwd()), "stdin": prompt}),
    encoding="utf-8",
)
if args != ["exec", "--json", "-"]:
    raise SystemExit(2)
if count == 1:
    os.kill(os.getppid(), signal.SIGKILL)
    raise SystemExit(0)
match = re.search(r'"run_id": "(run-[0-9a-f]+)"', prompt)
if match is None:
    raise SystemExit(3)
relative = f"artifacts/runs/{match.group(1)}/tests.json"
submission = {
    "updates": [{
        "item_id": "verify",
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
}
print(json.dumps({
    "thread_id": "smoke-codex-session",
    "item": {"type": "agent_message", "text": json.dumps(submission, sort_keys=True)},
}))
""",
        encoding="utf-8",
    )
    codex.chmod(0o755)


def _json_action_submission(
    env: dict[str, str], action: dict[str, Any], payload: dict[str, object], *, cwd: Path
) -> dict[str, Any]:
    stdin_contract = _object(_object(action, "submit"), "stdin")
    _require(
        stdin_contract == {"format": "json", "source": "payload"},
        "action stdin contract is invalid",
    )
    return _json_argv(
        env,
        _action_argv(action, "submit"),
        cwd=cwd,
        input_text=json.dumps(payload),
    )


def _action_argv(action: dict[str, Any], key: str) -> list[str]:
    descriptor = _object(action, key)
    raw = descriptor.get("argv")
    command = descriptor.get("command")
    if not isinstance(raw, list) or not raw or not all(isinstance(value, str) for value in raw):
        raise AssertionError(f"invalid action argv: {key}")
    argv = list(raw)
    if argv[0] != "twin" or command != shlex.join(argv):
        raise AssertionError(f"action command does not match argv: {key}")
    return argv


def _json_command(
    twin: Path,
    env: dict[str, str],
    *args: str,
    cwd: Optional[Path] = None,
    input_text: Optional[str] = None,
) -> dict[str, Any]:
    return _json_result(
        _command(twin, env, *args, cwd=cwd, input_text=input_text),
        " ".join(args),
    )


def _json_argv(
    env: dict[str, str],
    argv: list[str],
    *,
    cwd: Optional[Path] = None,
    input_text: Optional[str] = None,
) -> dict[str, Any]:
    return _json_result(
        _command_argv(env, argv, cwd=cwd, input_text=input_text),
        shlex.join(argv),
    )


def _json_result(result: subprocess.CompletedProcess[str], label: str) -> dict[str, Any]:
    if result.returncode != 0:
        raise AssertionError(f"Twin command failed ({label}): {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"Twin command did not emit JSON ({label}): {result.stdout!r}") from exc
    if not isinstance(payload, dict):
        raise AssertionError(f"Twin command emitted a non-object result: {label}")
    return payload


def _command(
    twin: Path,
    env: dict[str, str],
    *args: str,
    cwd: Optional[Path] = None,
    input_text: Optional[str] = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(twin), *args],
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _command_argv(
    env: dict[str, str],
    argv: list[str],
    *,
    cwd: Optional[Path] = None,
    input_text: Optional[str] = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AssertionError(f"missing persisted JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise AssertionError(f"persisted JSON is not an object: {path}")
    return payload


def _object(value: dict[str, Any], key: str) -> dict[str, Any]:
    child = value.get(key)
    if not isinstance(child, dict):
        raise AssertionError(f"missing object field: {key}")
    return child


def _string(value: dict[str, Any], key: str) -> str:
    child = value.get(key)
    if not isinstance(child, str) or not child:
        raise AssertionError(f"missing string field: {key}")
    return child


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"SMOKE FAILURE: {exc}", file=sys.stderr)
        raise SystemExit(1)
