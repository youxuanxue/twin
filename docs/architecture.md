# Twin architecture

Twin keeps coordination state outside the target repository and treats each command as
a small, token-bound transition. Its four layers are deliberately narrow:

1. **CLI and contract** expose the public commands and the machine-readable discovery
   document. Action-only submission commands are handed back only in issued actions.
2. **Domain service** owns lifecycle transitions, action-token validation, direct worker
   submission, evidence requirements, review decisions, and human responses.
3. **Storage** owns durable workspace documents, revision checks, event history, locks,
   and recovery of interrupted writes beneath `~/.twin`.
4. **Runtime and resources** load `~/.twin/config.toml`, prepare an isolated worker
   checkout, invoke the selected local-CLI or CAO adapter, and load packaged contracts.

The target repository is workspace input, never Twin's state directory. A workspace
records its target root as metadata while its goal, plan, state, artifacts, and events
remain in `~/.twin/workspaces`. The active-workspace mapping is also home-scoped, so a
repository does not gain hidden Twin state.

Supervisor routes and worker providers are independent. A route such as
`host/antigravity` identifies the host consuming actions; runtime configuration selects
`claude`, `codex`, or `gemini` as the worker. `run` persists a token-free
`runs/<run_id>/request.json` before execution, resumes that same request after a crash,
then atomically publishes `result.json`, `evidence.json`, controlled artifacts, plan
updates, and the review action. The host never submits worker output.

Every workspace read and mutation passes one integrity validator under the workspace
lock. It binds meta/state/goal/plan identities, event revisions, pending actions, run
records, and artifact hashes. Explicit workspace commands must match the repository root
recorded in `meta.json`.

`setup` copies the packaged Twin skill into `~/.twin/skills/twin`, then adds only the
Twin-owned host entries. The Cursor skills directory remains a real additive registry;
foreign entries are preserved. A deterministic manifest lets `setup --check` and
`doctor` reject missing, extra, changed, or symlinked installed skill content. The
installed command uses packaged resources rather than a source checkout.
