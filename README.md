# Twin

Twin is a provider-neutral supervisor for evidence-driven agent work.

Python 3.9 or newer is required.

Install the packaged CLI with:

```bash
python3 -m pip install xuejiao-twin
```

For a source checkout, run the release gate before publishing changes:

```bash
TWIN_REQUIRE_CONTAINER=1 bash scripts/preflight.sh
```

See `docs/operator-guide.md` for setup and recovery, `docs/architecture.md` for
the data boundary, and `docs/agent-integration.md` for the generated agent contract.
