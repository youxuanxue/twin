<!-- Generated from `twin contract --json`; do not edit by hand. -->

# Twin agent integration

Agents discover Twin only through `twin contract --json`, then invoke the exact
console command named there. Submission tokens and schema paths are emitted at runtime
and must be consumed literally.

## Commands

- `start`: `start <goal> --supervisor host/<provider> --json`
  - output: `action`
  - schema: `schemas/twin.action.schema.json`
- `run`: `run [workspace] --supervisor host/<provider> --json`
  - output: `action`
  - schema: `schemas/twin.action.schema.json`
- `status`: `status [workspace] [--json]`
  - output: `workspace-result`
- `respond`: `respond <answer> [--workspace <id>] [--json]`
  - output: `workspace-result`
- `handoff`: `handoff <workspace> --from host/<provider> --to host/<provider> --json`
  - output: `workspace-result`
- `submit-plan`: `submit-plan --workspace <id> --supervisor host/<provider> --state-revision <int> --action-token <token> --payload-file - --json`
  - output: `workspace-result`
- `submit-instruction`: `submit-instruction --workspace <id> --supervisor host/<provider> --state-revision <int> --action-token <token> --run-id <id> --payload-file - --json`
  - output: `action`
  - schema: `schemas/twin.action.schema.json`
- `submit-review`: `submit-review --workspace <id> --supervisor host/<provider> --state-revision <int> --action-token <token> --run-id <id> --payload-file - --json`
  - output: `workspace-result`

## Action-only submissions

The following commands are intentionally omitted from interactive help and are returned
only as action handoffs: `submit-plan`, `submit-instruction`, `submit-review`.
