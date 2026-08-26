---
name: twin
description: Use when a user explicitly invokes Twin to supervise a goal or continue a Twin workspace.
---

# Twin

Use only the installed `twin` CLI.

1. Run `twin doctor --json` as the health and capability gate. It does not discover commands or actions.
2. Run `twin contract --json` as the sole command and action discovery surface.
3. From the contract, use `start`, `run`, `status`, `respond`, and `handoff` exactly as declared. The `host/codex`, `host/claude`, or `host/antigravity` route identifies the supervisor host only; `~/.twin/config.toml` independently selects the worker adapter and provider.
4. For every self-describing action, read `context` and `expected_output`, send the JSON payload to `submit.argv` on stdin, and execute that argv exactly. Continue from the workspace result returned by the submission.
5. For every workspace result, execute `next_command.argv` exactly when `next_command` is non-null, then apply this same rule to the returned result. Stop when it is null; never derive a command from `status`.
6. `run` owns worker execution and worker submission. Wait for its review action; never construct a worker action, ingest worker output yourself, or write Twin artifacts/state.

Never read a source checkout, reconstruct argv or tokens, reproduce schema fields, or edit Twin state.
