from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Mapping, Sequence

from twin.runtime.process import ProcessRunner
from twin.runtime.protocols import (
    WorkerTurnRequest,
    WorkerTurnResult,
    clean_worker_environment,
)


_LOCAL_PROVIDERS = frozenset({"claude", "codex", "gemini", "claude_headless"})
_BUDGET_KEYS = frozenset({"MAX_BUDGET_USD", "BUDGET_USD", "TWIN_BUDGET_USD"})
_PERMISSION_MODES = {
    "acceptEdits": "acceptEdits",
    "bypassPermissions": "bypassPermissions",
    "default": "",
}


class LocalCliRuntime:
    def __init__(
        self,
        *,
        executables: Mapping[str, Sequence[str] | str] | None = None,
        process_runner: ProcessRunner | None = None,
    ) -> None:
        self.executables = dict(executables or {})
        self.process_runner = process_runner or ProcessRunner()

    def run_turn(self, request: WorkerTurnRequest) -> WorkerTurnResult:
        provider = request.provider
        if provider not in _LOCAL_PROVIDERS:
            return self._failure("provider_not_found", f"provider is not supported: {provider}", request)
        if provider != "claude_headless" and any(key in request.environment for key in _BUDGET_KEYS):
            return self._failure("unsupported_budget", f"provider does not support budgets: {provider}", request)
        permission_args = self._permission_args(provider, request.environment)
        if permission_args is None:
            return self._failure("unsupported_permission_mode", f"unsupported permission mode: {provider}", request)
        command = self._command(provider, request.prompt, permission_args)
        if command is None:
            return self._failure("provider_not_found", f"provider executable not found: {provider}", request)
        try:
            process = self.process_runner.run(
                command,
                cwd=request.cwd,
                environment=self._environment(request.environment),
                timeout_seconds=request.timeout_seconds,
                input_text=request.prompt,
            )
        except FileNotFoundError:
            return self._failure("provider_not_found", f"provider executable not found: {provider}", request)
        if process.timed_out:
            return WorkerTurnResult(
                output_text="provider timed out",
                returncode=process.returncode,
                session_id=request.session_id,
                events=({"event": "failure", "failure_kind": "timeout", "provider": provider},),
                timed_out=True,
            )
        parsed = self._parse_provider_output(provider, process.stdout, request.session_id)
        if parsed is None:
            return WorkerTurnResult(
                output_text="malformed provider output",
                returncode=1,
                session_id=request.session_id,
                events=({"event": "failure", "failure_kind": "malformed_output", "provider": provider},),
            )
        output_text, session_id, events = parsed
        if process.returncode != 0:
            events = (
                *events,
                {"event": "failure", "failure_kind": "returncode", "provider": provider, "returncode": process.returncode},
            )
        return WorkerTurnResult(
            output_text=output_text,
            returncode=process.returncode,
            session_id=session_id or request.session_id,
            events=events,
        )

    def _command(self, provider: str, prompt: str, permission_args: list[str]) -> list[str] | None:
        configured = self.executables.get(provider)
        if configured is not None:
            if isinstance(configured, str):
                return [configured, *permission_args]
            return [str(part) for part in configured] + permission_args
        executable = "claude" if provider == "claude_headless" else provider
        resolved = shutil.which(executable)
        if resolved is None:
            return None
        if provider == "claude_headless":
            return [resolved, *permission_args, "-p", prompt, "--output-format", "stream-json"]
        if provider == "codex":
            return [resolved, "exec", "--json", prompt]
        if provider == "gemini":
            return [resolved, "-p", prompt, "--json"]
        return [resolved, *permission_args, "-p", prompt, "--output-format", "stream-json"]

    @staticmethod
    def _permission_args(provider: str, environment: Mapping[str, str]) -> list[str] | None:
        raw_mode = environment.get("TWIN_PERMISSION_MODE") or environment.get("CLAUDE_PERMISSION_MODE")
        if raw_mode is None:
            return []
        mode = _PERMISSION_MODES.get(raw_mode)
        if mode is None or provider not in {"claude", "claude_headless"}:
            return None
        if mode == "":
            return []
        return ["--permission-mode", mode]

    @staticmethod
    def _environment(environment: Mapping[str, str]) -> dict[str, str]:
        merged = {
            str(key): str(value)
            for key, value in os.environ.items()
            if key not in {"DEV_RULES", "PERSONA_PATH", "TWIN_PERSONA_PATH"}
        }
        merged.update(clean_worker_environment(environment))
        return merged

    @staticmethod
    def _failure(kind: str, message: str, request: WorkerTurnRequest) -> WorkerTurnResult:
        return WorkerTurnResult(
            output_text=message,
            returncode=1,
            session_id=request.session_id,
            events=({"event": "failure", "failure_kind": kind, "provider": request.provider},),
        )

    def _parse_provider_output(
        self, provider: str, stdout: str, default_session_id: str
    ) -> tuple[str, str, tuple[dict[str, object], ...]] | None:
        records: list[dict[str, object]] = []
        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                return None
            if not isinstance(value, dict):
                return None
            records.append(value)
        if not records:
            return "", default_session_id, ()
        if provider in {"claude", "claude_headless"}:
            return self._parse_claude_records(records, default_session_id)
        return self._parse_json_records(records, default_session_id)

    @staticmethod
    def _parse_json_records(
        records: list[dict[str, object]], default_session_id: str
    ) -> tuple[str, str, tuple[dict[str, object], ...]]:
        texts: list[str] = []
        session_id = default_session_id
        for record in records:
            for key in ("output_text", "text", "message", "content"):
                value = record.get(key)
                if isinstance(value, str):
                    texts.append(value)
                    break
            value = record.get("session_id") or record.get("sessionId")
            if isinstance(value, str):
                session_id = value
        return "\n".join(texts), session_id, tuple(records)

    @staticmethod
    def _parse_claude_records(
        records: list[dict[str, object]], default_session_id: str
    ) -> tuple[str, str, tuple[dict[str, object], ...]]:
        texts: list[str] = []
        session_id = default_session_id
        for record in records:
            value = record.get("session_id") or record.get("sessionId")
            if isinstance(value, str):
                session_id = value
            result = record.get("result")
            if isinstance(result, str):
                texts.append(result)
            message = record.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and isinstance(block.get("text"), str):
                            texts.append(block["text"])
                elif isinstance(content, str):
                    texts.append(content)
            content = record.get("content")
            if isinstance(content, str):
                texts.append(content)
        return "\n".join(texts), session_id, tuple(records)
