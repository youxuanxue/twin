---
name: twin
description: Use when a user explicitly invokes Twin to supervise a goal or continue a Twin workspace.
---

# Twin

Use only the installed `twin` CLI.

1. Run `twin doctor --json` as the health and capability gate. It does not discover commands or actions.
2. Run `twin contract --json` as the sole command and action discovery surface.
3. From the contract, use `start` for a new goal, `run` for a workspace, `status` for status, and `respond` for an answer. Select `host/codex`, `host/claude`, or `host/antigravity` for the current host.
4. For every self-describing action, consume `context`, `expected_output`, `submit.command`, and `next_command` literally.

Never read a source checkout, construct tokens, reproduce schema fields, or edit Twin state.
