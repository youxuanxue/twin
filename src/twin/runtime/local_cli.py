from __future__ import annotations

import json
import os
import shutil
from typing import Mapping, Sequence

from twin.runtime.process import ProcessRunner
from twin.runtime.protocols import (
    WorkerTurnRequest,
    WorkerTurnResult,
    parse_worker_submission,
    worker_process_environment,
)


PROVIDER_CONTRACT_VERSION = 1
_LOCAL_PROVIDERS = frozenset({"claude", "codex", "gemini"})
_BUDGET_KEYS = frozenset({"MAX_BUDGET_USD", "BUDGET_USD", "TWIN_BUDGET_USD"})
CLAUDE_PERMISSION_MODES = frozenset({
    "acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan",
})


class LocalCliRuntime:
    contract_version = PROVIDER_CONTRACT_VERSION

    def __init__(
        self,
        *,
        executables: Mapping[str, Sequence[str] | str] | None = None,
        process_runner: ProcessRunner | None = None,
        claude_allowed_tools: Sequence[str] | None = None,
        claude_max_budget_usd: float | int | None = None,
        claude_permission_mode: str | None = None,
    ) -> None:
        self.executables = dict(executables or {})
        self.process_runner = process_runner or ProcessRunner()
        self.claude_allowed_tools = tuple(str(value) for value in (claude_allowed_tools or ()))
        self.claude_max_budget_usd = (
            None if claude_max_budget_usd is None else float(claude_max_budget_usd)
        )
        self.claude_permission_mode = claude_permission_mode

    def run_turn(self, request: WorkerTurnRequest) -> WorkerTurnResult:
        provider = request.provider
        if provider not in _LOCAL_PROVIDERS:
            return self._failure(
                "provider_not_found", f"provider is not supported: {provider}", request
            )
        if any(key in request.environment for key in _BUDGET_KEYS):
            return self._failure(
                "unsupported_budget",
                "worker budgets must be declared in Twin runtime configuration",
                request,
            )
        controls_error = self._controls_error(provider)
        if controls_error is not None:
            return self._failure("missing_required_control", controls_error, request)
        invocation = self._invocation(provider, request.prompt)
        if invocation is None:
            return self._failure(
                "provider_not_found", f"provider executable not found: {provider}", request
            )
        command, input_text = invocation
        try:
            process = self.process_runner.run(
                command,
                cwd=request.cwd,
                environment=self._environment(request.environment),
                timeout_seconds=request.timeout_seconds,
                input_text=input_text,
            )
        except FileNotFoundError:
            return self._failure(
                "provider_not_found", f"provider executable not found: {provider}", request
            )
        if process.timed_out:
            return WorkerTurnResult(
                output_text="provider timed out",
                returncode=process.returncode,
                session_id=request.session_id,
                events=({
                    "event": "failure", "failure_kind": "timeout", "provider": provider,
                },),
                timed_out=True,
            )
        parsed = self._parse_provider_output(provider, process.stdout, request.session_id)
        if parsed is None:
            return WorkerTurnResult(
                output_text="malformed provider output",
                returncode=1,
                session_id=request.session_id,
                events=({
                    "event": "failure", "failure_kind": "malformed_output", "provider": provider,
                },),
            )
        output_text, session_id, events = parsed
        if process.returncode != 0:
            events = (*events, {
                "event": "failure",
                "failure_kind": "returncode",
                "provider": provider,
                "returncode": process.returncode,
            })
        return WorkerTurnResult(
            output_text=output_text,
            returncode=process.returncode,
            session_id=session_id or request.session_id,
            events=events,
            submission=parse_worker_submission(output_text),
        )

    def _invocation(self, provider: str, prompt: str) -> tuple[list[str], str | None] | None:
        base = self._base_command(provider)
        if base is None:
            return None
        if provider == "codex":
            return [*base, "exec", "--json", "-"], prompt
        if provider == "gemini":
            return [*base, "-p", prompt, "--output-format", "json"], None
        tools = ",".join(self.claude_allowed_tools)
        assert self.claude_max_budget_usd is not None
        argv = [
            *base,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--allowedTools", tools,
            "--max-budget-usd", format(self.claude_max_budget_usd, "g"),
        ]
        if self.claude_permission_mode is not None:
            argv.extend(("--permission-mode", self.claude_permission_mode))
        return argv, prompt

    def _base_command(self, provider: str) -> list[str] | None:
        configured = self.executables.get(provider)
        if configured is not None:
            if isinstance(configured, str):
                return [configured]
            return [str(part) for part in configured]
        resolved = shutil.which(provider)
        return None if resolved is None else [resolved]

    def _controls_error(self, provider: str) -> str | None:
        if provider != "claude":
            return None
        if not self.claude_allowed_tools:
            return "Claude headless requires configured allowed tools"
        if self.claude_max_budget_usd is None or self.claude_max_budget_usd <= 0:
            return "Claude headless requires a positive max budget"
        if (
            self.claude_permission_mode is not None
            and self.claude_permission_mode not in CLAUDE_PERMISSION_MODES
        ):
            return "Claude headless has an unsupported permission mode"
        return None

    @staticmethod
    def _environment(environment: Mapping[str, str]) -> dict[str, str]:
        return worker_process_environment(os.environ, environment)

    @staticmethod
    def _failure(kind: str, message: str, request: WorkerTurnRequest) -> WorkerTurnResult:
        return WorkerTurnResult(
            output_text=message,
            returncode=1,
            session_id=request.session_id,
            events=({
                "event": "failure", "failure_kind": kind, "provider": request.provider,
            },),
        )

    def _parse_provider_output(
        self, provider: str, stdout: str, default_session_id: str
    ) -> tuple[str, str, tuple[dict[str, object], ...]] | None:
        if provider == "gemini":
            try:
                value = json.loads(stdout)
            except json.JSONDecodeError:
                return None
            if not isinstance(value, dict):
                return None
            output = value.get("response") or value.get("output_text") or value.get("text")
            session_id = value.get("session_id") or value.get("sessionId") or default_session_id
            if not isinstance(output, str) or not isinstance(session_id, str):
                return None
            return output, session_id, (value,)

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
            return None
        if provider == "claude":
            return self._parse_claude_records(records, default_session_id)
        return self._parse_codex_records(records, default_session_id)

    @staticmethod
    def _parse_codex_records(
        records: list[dict[str, object]], default_session_id: str
    ) -> tuple[str, str, tuple[dict[str, object], ...]]:
        texts: list[str] = []
        session_id = default_session_id
        for record in records:
            thread_id = record.get("thread_id")
            if isinstance(thread_id, str):
                session_id = thread_id
            value = record.get("session_id") or record.get("sessionId")
            if isinstance(value, str):
                session_id = value
            item = record.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    texts.append(text)
                    continue
            for key in ("output_text", "text", "message", "content"):
                value = record.get(key)
                if isinstance(value, str):
                    texts.append(value)
                    break
        return "\n".join(texts), session_id, tuple(records)

    @staticmethod
    def _parse_claude_records(
        records: list[dict[str, object]], default_session_id: str
    ) -> tuple[str, str, tuple[dict[str, object], ...]]:
        texts: list[str] = []
        terminal_result: str | None = None
        session_id = default_session_id
        for record in records:
            value = record.get("session_id") or record.get("sessionId")
            if isinstance(value, str):
                session_id = value
            result = record.get("result")
            if isinstance(result, str):
                terminal_result = result
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
        output_text = terminal_result if terminal_result is not None else "\n".join(texts)
        return output_text, session_id, tuple(records)
