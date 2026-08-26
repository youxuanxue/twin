# Twin operator guide

## Install and set up

Twin requires Python 3.9 or newer. Install the package, then use its console script:

```bash
python3 -m pip install xuejiao-twin
twin setup
twin doctor --json
```

`setup` installs Twin's packaged skill below `~/.twin/skills/twin` and creates its
host entries without replacing foreign skills. `twin setup --check --json` verifies the
links without changing them.

## Discover and operate

Use `twin contract --json` for command and action discovery. Start a workspace with a
supervisor route, submit the plan action it returns, then use `run`, review actions,
`respond`, `status`, and `handoff` exactly as their JSON results require. Do not create
or replay action tokens and do not edit workspace state directly.

## Diagnose providers and recovery

`twin doctor --json` distinguishes required installation checks from optional local
provider availability. Missing optional provider binaries do not invalidate the package
or its workspace store. Workspace writes are revision-bound and journaled; a later
command recovers an interrupted write before reading or changing the workspace.

If a worker checkout is dirty or contains an unintegrated commit, Twin preserves it.
Resolve the worktree deliberately rather than deleting it as part of a retry.

## Uninstall

Run `twin uninstall --json` to remove only Twin-owned host links and the installed Twin
skill copy. Foreign entries and the shared Cursor registry are retained. Workspace data
under `~/.twin` is not removed by skill uninstall.
