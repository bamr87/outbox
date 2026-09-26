# SCHEMA — bamr87/outbox

| Path | Placement | Forbidden |
| --- | --- | --- |
| `src/outbox/` | Protocol, mailbox, arena, policy agents, CLI | Live secrets, trial run output |
| `tasks/` | Frozen task cards used by tests | Generated ledgers |
| `agents/` | System cards and Grok Bot kickoff | Code |
| `protocol/` | JSON Schema for messages | Runtime state |
| `tests/` | Unittest suite | Network calls |
| `runs/` | Local trial output (gitignored) | Committed fixtures unless under `tests/` |
| `.github/workflows/` | Offline unit + suite smoke | Deploy |

New directories need a row here in the same change.
