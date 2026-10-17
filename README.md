# outbox

> Async competitive outbox harness for three agents: mailbox protocol, injected lag, confidence race and replay scorer.

[![License](https://img.shields.io/github/license/bamr87/outbox)](LICENSE)

## Overview

Competitive asynchronous outbound messaging for three Grok-class agents.

Each agent may only publish by writing an immutable outbox record. Peers never mutate each other's working memory. A dumb observer watches who published, when, with what confidence, and whether later environment state actually caught up.

This repo is the measurable harness from the "how would you test three Grok bots" design: filesystem inbox/outbox, injected environment lag, a confidence race, and a replay scorer.

## Features

- Agents publish only by writing immutable outbox records; peers never mutate each other's memory
- Three agents with distinct roles and blind spots: a scout and two claimants
- Injected environment lag and a confidence race between the claimants
- A replay scorer and watch column that judge who was right, and when
- Offline policy bots stand in for live Grok agents

## Agents

| Agent | Role | Allowed types | Blind spot |
| --- | --- | --- | --- |
| `scout` | environment / clock | `env_event`, `deadline_ping` | cannot claim |
| `claimant-a` | fast, high temperature | `claim`, `retract` | no patience; optional sticky first answer |
| `claimant-b` | slow, tool-shaped | `claim`, `retract` | inbox lag `lag_b` ticks |

Policy bots stand in for Grok so the suite runs offline. Live Grok Bots use the cards in `agents/` and the kickoff in `agents/grok_bots_kickoff.md`.

## Quick start

```bash
python3 -m pip install -e .
python3 -m outbox.cli run --task tasks/stale_fact.json --out .
python3 -m outbox.cli watch --ledger runs/*/ledger.jsonl
python3 -m outbox.cli suite --tasks tasks --out .
python3 -m unittest discover -s tests -v
```

`run` prints a result object:

```json
{
  "task_id": "T-stale-fact",
  "first_claim_from": "claimant-a",
  "winner": {"from": "claimant-a", "answer": "green", "confidence": 0.86, "env_seq": 2},
  "correct": true,
  "confidence_inversion": false,
  "retracts": 2
}
```

## Task families

- `tasks/stale_fact.json` — environment flips; A claims first; someone must retract; current env should win.
- `tasks/ambiguous.json` — A is sticky on the first wrong source; high confidence loses. This is the inversion demo.
- `tasks/deadline.json` — a better fact arrives inside B's lag window; B should miss it.

Edit `lag_b`, `a_sticky`, and `env_schedule` to widen or close the race.

## Protocol

Messages are JSON files matching `protocol/schema.json`.

Rules the code enforces:

1. Write the claim (or env event) into the sender outbox, then a relay copies it to recipient inboxes and `ledger.jsonl`.
2. Peers learn only by reading their inbox. `state.json` is private.
3. At `closes_at_tick`, the winner is the highest-confidence non-retracted claim whose `env_seq` is current (`max_env_lag` may relax that).
4. A judge field `expected_answer` scores correctness after close. The observer does not synthesize during the race.

Invalid outbox files go to `dead_letter.jsonl` instead of silently infecting inboxes.

## Project structure
```
arena/
  task.json
  ledger.jsonl
  dead_letter.jsonl
  agents/scout|claimant-a|claimant-b/
    state.json          # private
    inbox/              # incoming
    inbox/archive/
    outbox/             # committed intent
```

## Watch column

```
t+0   task_open  What is the live status code for service hydra?
t+0   env=1      hydra status probe returns amber
t+0   claimant-a claim  conf=0.79  env=1  ans=amber
t+4   env=2      hydra flipped to green after failover
t+4   claimant-a retract
t+4   claimant-a claim  conf=0.86  env=2  ans=green
t+7   claimant-b claim  conf=0.67  env=2  ans=green
```

## Falsifiers

The idea fails if bots only talk in prose, if the highest confidence is systematically wrong after an env update, if winners are always the last speaker, if agents cite facts that never entered their inbox, or if retracts never happen.

## Development

```bash
python3 -m pip install -e .
python3 -m pytest
```

Full commands, conventions and gotchas for humans and AI agents live in [AGENTS.md](AGENTS.md).

## Contributing

Contributions are welcome via pull request. Use [Conventional Commits](https://www.conventionalcommits.org/) and keep the CI checks green. The fleet-wide workflow is described in the hub's [CONTRIBUTING.md](https://github.com/bamr87/bamr87/blob/main/CONTRIBUTING.md). AI agents should start with [AGENTS.md](AGENTS.md).

## Changelog

There is no CHANGELOG.md yet; changes are tracked in the [commit history](https://github.com/bamr87/outbox/commits/main).

## License

Released under the [MIT License](LICENSE).

---

<!-- bamr87-portfolio-footer -->
<sub>Part of the [bamr87 portfolio](https://github.com/bamr87) — browse every project on [Amr's GitHub profile](https://github.com/bamr87).</sub>
