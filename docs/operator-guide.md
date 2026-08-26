# Twin operator guide

## Install and set up

Twin requires Python 3.9 or newer. Install the package, then use its console script:

```bash
python3 -m pip install xuejiao-twin
twin setup
twin doctor --json
```

`setup` installs Twin's packaged skill below `~/.twin/skills/twin` and creates its
host entries without replacing foreign skills. `twin setup --check --json` verifies both
the links and the installed skill manifest without changing them.

Before `run`, create `~/.twin/config.toml`. A local Codex worker needs only:

```toml
[runtime]
adapter = "local_cli"
worker_provider = "codex"
timeout_seconds = 300
```

`worker_provider` may be `claude`, `codex`, or `gemini`. Claude additionally requires
`[local_cli]` values for `claude_allowed_tools` and `claude_max_budget_usd`. The `cao`
adapter requires a loopback or HTTPS endpoint, an auth-token environment variable,
provider, and agent. `twin doctor --json` validates the selected configuration.

## Discover and operate

Use `twin contract --json` for command and action discovery. Start a workspace with a
supervisor route, submit the plan action it returns, then use `run`, review actions,
`respond`, `status`, and `handoff` exactly as their JSON results require. Do not create
or replay action tokens and do not edit workspace state directly.

Execute each returned `submit.argv` with the requested JSON payload on stdin, then
continue from the workspace result returned by that submission. For every workspace
result, execute `next_command.argv` exactly when it is non-null and apply the same rule
to the returned result until it is null. Never derive a command from `status`. The
supervisor route identifies the host; it does not choose the worker provider. `run`
owns worker execution and submission and returns the review action directly.

For command evidence, declare an immutable path such as
`command:artifacts/runs/{run_id}/tests.json`. Twin materializes `{run_id}` in the worker
prompt and commits the successful command result with the plan update.

## Diagnose providers and recovery

`twin doctor --json` distinguishes required installation checks from optional local
provider availability. Missing optional provider binaries do not invalidate the package
or its workspace store. Workspace writes are revision-bound and journaled; a later
command recovers an interrupted write before reading or changing the workspace.

If a process dies in `worker_running`, rerun the same `twin run <workspace> ...` command.
Twin validates the persisted token-free request, resumes the same run ID, and never
retains an action token in the worker prompt or run evidence.

If a worker checkout is dirty or contains an unintegrated commit, Twin preserves it.
Resolve the worktree deliberately rather than deleting it as part of a retry.

## Uninstall

Run `twin uninstall --json` to remove only Twin-owned host links and the installed Twin
skill copy. Foreign entries and the shared Cursor registry are retained. Workspace data
under `~/.twin` is not removed by skill uninstall.
