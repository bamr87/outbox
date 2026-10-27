# SCHEMA — bamr87/outbox

> Where each kind of file lives in this repository, what is forbidden there, and the rule for adding a directory.

| Path | Placement | Forbidden |
| --- | --- | --- |
| `src/outbox/` | Protocol, mailbox, arena, policy agents, belt, claim desk, CLI | Live secrets, trial run output |
| `tasks/` | Frozen task cards used by tests | Generated ledgers |
| `agents/` | System cards and Grok Bot kickoff | Code |
| `protocol/` | JSON Schemas for messages: arena (`schema.json`) and belt (`belt.schema.json`) | Runtime state |
| `e2e/` | The local end-to-end proof of the platform plan: `run.py`, adapters that run sibling repos' tooling on snapshots, fixtures, blueprints | Writes into sibling checkouts, committed run output, secrets |
| `tests/` | Unittest suite | Network calls |
| `tools/` | Repository scripts: the doc generators (`gen_cli_reference.py`, the vendored `docs_index.py`) | Runtime code, edits to the vendored `docs_index.py` |
| `docs/` | The docs index (`README.md`), ADRs in `adr/`, generated reference in `reference/` | Hand edits to generated files |
| `features/` | The fleet feature index, `features.yml` (schema features/v1) | — |
| `verify/` | User scenarios and the runner for the fleet verify gate | — |
| `public/` | The vendored fleet page-feedback widget | Hand edits to the vendored scripts |
| `runs/` | Local trial output (gitignored) | Committed fixtures unless under `tests/` |
| `.github/workflows/` | Offline unit + suite smoke | Deploy |

New directories need a row here in the same change.
