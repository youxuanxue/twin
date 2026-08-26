# Twin architecture

Twin keeps coordination state outside the target repository and treats each command as
a small, token-bound transition. Its four layers are deliberately narrow:

1. **CLI and contract** expose the public commands and the machine-readable discovery
   document. Action-only submission commands are handed back only in issued actions.
2. **Domain service** owns lifecycle transitions, action-token validation, evidence
   requirements, review decisions, and human responses.
3. **Storage** owns durable workspace documents, revision checks, event history, locks,
   and recovery of interrupted writes beneath `~/.twin`.
4. **Runtime and resources** prepare an isolated worker checkout, invoke a selected
   local provider, and load schemas, personas, templates, and the packaged host skill.

The target repository is workspace input, never Twin's state directory. A workspace
records its target root as metadata while its goal, plan, state, artifacts, and events
remain in `~/.twin/workspaces`. The active-workspace mapping is also home-scoped, so a
repository does not gain hidden Twin state.

`setup` copies the packaged Twin skill into `~/.twin/skills/twin`, then adds only the
Twin-owned host entries. The Cursor skills directory remains a real additive registry;
foreign entries are preserved. The installed command uses packaged resources rather
than a source checkout.
