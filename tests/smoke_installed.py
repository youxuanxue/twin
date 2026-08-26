"""Drive Twin's installed-wheel lifecycle without importing Twin source code."""
from __future__ import annotations

import json
import os
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
        _install_fake_git(root, env)
        _install_fake_codex(root)

        _json_command(twin, env, "setup", "--json")
        contract = _json_command(twin, env, "contract", "--json")
        _require("submit-plan" in contract["action_commands"], "contract omitted submit-plan")
        doctor = _json_command(twin, env, "doctor", "--json")
        _require(doctor["checks"]["package_resources"]["ok"], "installed resources are unhealthy")

        started = _json_command(
            twin, env, "start", "ship the smoke lifecycle", "--supervisor", "host/codex", "--json",
            cwd=repo,
        )
        workspace = _string(started, "workspace")
        _json_command(
            twin, env,
            "submit-plan", "--workspace", workspace, "--supervisor", "host/codex",
            "--state-revision", str(started["state_revision"]),
            "--action-token", _string(started, "action_token"), "--payload-file", "-", "--json",
            cwd=repo, input_text=json.dumps(_plan_payload()),
        )
        reviewed = _json_command(
            twin, env, "run", workspace, "--supervisor", "host/codex", "--json", cwd=repo,
        )
        _require(reviewed["action"] == "review", "run did not issue a review action")
        run_id = _string(_object(_object(reviewed, "context"), "metadata"), "run_id")

        run_root = home / ".twin" / "workspaces" / workspace / "runs" / run_id
        result_payload = _json_file(run_root / "result.json")
        _require(result_payload["returncode"] == 0, "worker runtime did not succeed")
        _require(result_payload["output_text"] == "smoke codex completed", "worker output was unexpected")
        evidence_payload = _json_file(run_root / "evidence.json")
        _require(evidence_payload["status"] == "completed", "worker evidence was not completed")

        worktree = repo.parent / f"{repo.name}-twin-{workspace}"
        _require(worktree.is_dir(), "dirty worker worktree did not survive Twin run")
        _require((worktree / "dirty-preserve.txt").is_file(), "dirty worker file was not preserved")
        invocation = _json_file(root / "bin" / "codex-invocation.json")
        _require(
            invocation["argv"][:2] == ["exec", "--json"],
            "codex did not receive the expected protocol",
        )
        _require(
            len(invocation["argv"]) == 3 and "## Twin action" in invocation["argv"][2],
            "codex did not receive the worker prompt",
        )
        _require(invocation["cwd"] == str(worktree), "codex did not run in the isolated worktree")

        needs_human = _json_command(
            twin, env,
            "submit-review", "--workspace", workspace, "--supervisor", "host/codex",
            "--state-revision", str(reviewed["state_revision"]),
            "--action-token", _string(reviewed, "action_token"), "--run-id", run_id,
            "--payload-file", "-", "--json", cwd=repo,
            input_text=json.dumps({"decision": "needs_human"}),
        )
        _require(needs_human["status"] == "needs_human", "review did not request a human")
        resumed = _json_command(
            twin, env, "respond", "continue", "--workspace", workspace, "--json", cwd=repo,
        )
        _require(resumed["status"] == "ready", "human response did not resume work")
        handed_off = _json_command(
            twin, env,
            "handoff", workspace, "--from", "host/codex", "--to", "host/claude", "--json",
            cwd=repo,
        )
        _require(handed_off["supervisor_route"] == "host/claude", "handoff did not persist")

        restarted = _json_command(twin, env, "status", workspace, "--json", cwd=repo)
        _require(restarted["status"] == "ready", "restart recovery did not preserve state")
        _require(restarted["supervisor_route"] == "host/claude", "restart recovery lost handoff")

        replay = _command(
            twin, env,
            "submit-plan", "--workspace", workspace, "--supervisor", "host/codex",
            "--state-revision", str(started["state_revision"]),
            "--action-token", _string(started, "action_token"), "--payload-file", "-", "--json",
            cwd=repo, input_text=json.dumps(_plan_payload()),
        )
        _require(replay.returncode != 0, "replayed action token was accepted")
        _require("stale or consumed action" in replay.stderr, "token replay was rejected for the wrong reason")

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
                {"id": "ac-1", "statement": "Lifecycle completes", "evidence_type": "artifact"},
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
                    "evidence_plan": ["artifacts/evidence.txt"],
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
import sys
from pathlib import Path


args = sys.argv[1:]
record = Path(__file__).with_name("codex-invocation.json")
record.write_text(json.dumps({"argv": args, "cwd": str(Path.cwd())}), encoding="utf-8")
if args[:2] != ["exec", "--json"] or len(args) != 3:
    raise SystemExit(2)
print(json.dumps({"session_id": "smoke-codex-session", "output_text": "smoke codex completed"}))
""",
        encoding="utf-8",
    )
    codex.chmod(0o755)


def _json_command(
    twin: Path, env: dict[str, str], *args: str, cwd: Optional[Path] = None,
    input_text: Optional[str] = None,
) -> dict[str, Any]:
    result = _command(twin, env, *args, cwd=cwd, input_text=input_text)
    if result.returncode != 0:
        raise AssertionError(
            f"Twin command failed ({' '.join(args)}): {result.stderr.strip()}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"Twin command did not emit JSON ({' '.join(args)}): {result.stdout!r}") from exc
    if not isinstance(payload, dict):
        raise AssertionError(f"Twin command emitted a non-object result: {' '.join(args)}")
    return payload


def _command(
    twin: Path, env: dict[str, str], *args: str, cwd: Optional[Path] = None,
    input_text: Optional[str] = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(twin), *args], cwd=str(cwd) if cwd is not None else None, env=env,
        input=input_text, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )


def _json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AssertionError(f"missing persisted run evidence: {path}") from exc
    if not isinstance(payload, dict):
        raise AssertionError(f"run evidence is not an object: {path}")
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
