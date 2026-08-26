from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from twin.runtime.cao import CaoRuntime
from twin.runtime.local_cli import CLAUDE_PERMISSION_MODES, LocalCliRuntime
from twin.runtime.protocols import WorkerRuntime


_ADAPTERS = frozenset({"local_cli", "cao"})
_PROVIDERS = frozenset({"claude", "codex", "gemini"})
_SECTIONS = {
    "runtime": {"adapter", "worker_provider", "timeout_seconds"},
    "local_cli": {
        "claude_allowed_tools", "claude_max_budget_usd", "claude_permission_mode",
    },
    "cao": {"endpoint", "auth_token_env", "provider", "agent"},
}


@dataclass(frozen=True)
class RuntimeConfig:
    adapter: str
    worker_provider: str
    timeout_seconds: float
    digest: str
    local_cli: Mapping[str, object]
    cao: Mapping[str, object]


def load_runtime_config(path: Path) -> RuntimeConfig:
    try:
        body = path.read_bytes()
    except FileNotFoundError as exc:
        raise ValueError(f"runtime configuration is missing: {path}") from exc
    parsed = _parse_toml(body.decode("utf-8"))
    runtime = parsed.get("runtime", {})
    local_cli = parsed.get("local_cli", {})
    cao = parsed.get("cao", {})
    adapter = runtime.get("adapter")
    provider = runtime.get("worker_provider")
    timeout = runtime.get("timeout_seconds", 300)
    if adapter not in _ADAPTERS:
        raise ValueError("runtime.adapter must be local_cli or cao")
    if provider not in _PROVIDERS:
        raise ValueError("runtime.worker_provider must be claude, codex, or gemini")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("runtime.timeout_seconds must be positive")
    if adapter == "local_cli":
        _validate_local_cli(provider, local_cli)
    else:
        _validate_cao(provider, cao)
    return RuntimeConfig(
        adapter=str(adapter),
        worker_provider=str(provider),
        timeout_seconds=float(timeout),
        digest=hashlib.sha256(body).hexdigest(),
        local_cli=dict(local_cli),
        cao=dict(cao),
    )


def build_runtime(
    config: RuntimeConfig, environment: Mapping[str, str] | None = None
) -> WorkerRuntime:
    if config.adapter == "local_cli":
        return LocalCliRuntime(
            claude_allowed_tools=_string_list(config.local_cli.get("claude_allowed_tools")),
            claude_max_budget_usd=_number(config.local_cli.get("claude_max_budget_usd")),
            claude_permission_mode=_optional_string(
                config.local_cli.get("claude_permission_mode")
            ),
        )
    values = config.cao
    token_name = str(values["auth_token_env"])
    token = (environment or os.environ).get(token_name)
    return CaoRuntime(
        str(values["endpoint"]),
        auth_token=token,
        provider=str(values["provider"]),
        agent=str(values["agent"]),
    )


def validate_provider_help(provider: str, transcript: str) -> list[str]:
    required = {
        "claude": ("--allowedTools", "--max-budget-usd", "--output-format", "-p, --print"),
        "codex": ("--json", "instructions are read from stdin", "if `-` is used"),
        "gemini": ("-p, --prompt", "--output-format", '"json"'),
    }.get(provider)
    if required is None:
        return [f"unknown provider: {provider}"]
    return [f"{provider} help is missing {value}" for value in required if value not in transcript]


def _parse_toml(text: str) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    section: str | None = None
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw).strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            if section not in _SECTIONS:
                raise ValueError(f"unsupported runtime config section: {section}")
            result.setdefault(section, {})
            continue
        if section is None or "=" not in line:
            raise ValueError(f"invalid runtime config line {line_number}")
        key, raw_value = (part.strip() for part in line.split("=", 1))
        if key not in _SECTIONS[section]:
            raise ValueError(f"unsupported runtime config key: {section}.{key}")
        if key in result[section]:
            raise ValueError(f"duplicate runtime config key: {section}.{key}")
        result[section][key] = _parse_value(raw_value, line_number)
    return result


def _parse_value(raw: str, line_number: int) -> object:
    if raw.startswith('"') or raw.startswith("["):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid runtime config value at line {line_number}") from exc
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1]
    if raw in {"true", "false"}:
        return raw == "true"
    try:
        return float(raw) if any(char in raw for char in ".eE") else int(raw)
    except ValueError as exc:
        raise ValueError(f"invalid runtime config value at line {line_number}") from exc


def _strip_comment(raw: str) -> str:
    quoted = False
    escaped = False
    for index, char in enumerate(raw):
        if char == "\\" and quoted:
            escaped = not escaped
            continue
        if char == '"' and not escaped:
            quoted = not quoted
        escaped = False
        if char == "#" and not quoted:
            return raw[:index]
    return raw


def _validate_local_cli(provider: object, values: Mapping[str, object]) -> None:
    if provider != "claude":
        return
    tools = values.get("claude_allowed_tools")
    budget = values.get("claude_max_budget_usd")
    if not isinstance(tools, list) or not tools or not all(
        isinstance(value, str) and value for value in tools
    ):
        raise ValueError("local_cli.claude_allowed_tools is required for Claude")
    if not isinstance(budget, (int, float)) or isinstance(budget, bool) or budget <= 0:
        raise ValueError("local_cli.claude_max_budget_usd is required for Claude")
    permission_mode = values.get("claude_permission_mode")
    if permission_mode is not None and (
        not isinstance(permission_mode, str)
        or permission_mode not in CLAUDE_PERMISSION_MODES
    ):
        raise ValueError("local_cli.claude_permission_mode is unsupported")


def _validate_cao(provider: object, values: Mapping[str, object]) -> None:
    for key in ("endpoint", "auth_token_env", "provider", "agent"):
        if not isinstance(values.get(key), str) or not str(values[key]):
            raise ValueError(f"cao.{key} is required")
    if values.get("provider") != provider:
        raise ValueError("cao.provider must match runtime.worker_provider")


def _string_list(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None
