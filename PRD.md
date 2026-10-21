# PRD — outbox

> Product requirements for [`bamr87/outbox`](https://github.com/bamr87/outbox).
> Derived from the README, agent docs, config and code in this repo. Anything not
> stated in those sources is marked **TBD**; success metrics are proposals.

## 1. Overview / problem

A measurable harness for competitive asynchronous outbound messaging among three agents. Agents may only publish by writing immutable outbox records; peers never mutate each other's memory. A filesystem inbox/outbox, injected environment lag, a confidence race and a replay scorer judge who published what, when, with what confidence, and whether they were right.

## 2. Target users

- Researchers/engineers evaluating multi-agent coordination and calibration
- Operators running live agent bots using the cards in `agents/`

## 3. Goals and non-goals

**Goals**

- Reproducible offline suite using policy bots in place of live agents
- Expose failure modes: stale facts, confidence inversion, lag-window misses
- Enforce a strict message protocol (`protocol/schema.json`)

**Non-goals**

- Network calls in tests
- Deploy workflows
- An observer that synthesizes answers during the race

## 4. Core features (current)

- Agents: scout (env/clock), claimant-a (fast), claimant-b (slow, lagged inbox)
- CLI: `run`, `watch`, `suite`
- Task families: stale_fact, ambiguous, deadline
- Relay to inboxes + `ledger.jsonl`; invalid messages to `dead_letter.jsonl`
- Scoring: winner, correctness, confidence inversion, retracts

## 5. Requirements

**Functional**

- Winner at `closes_at_tick` = highest-confidence non-retracted claim with current `env_seq` (subject to `max_env_lag`)
- Peers learn only via inbox; `state.json` is private
- Correctness judged after close against `expected_answer`

**Non-functional**

- Python ≥ 3.11; unittest suite runs offline
- Deterministic replays from ledgers

## 6. Success metrics (proposed — not defined in sources)

- Correct-winner rate per task family
- Confidence-inversion rate
- Retract latency after env change

## 7. Roadmap / open questions

- Live-agent evaluation protocol and acceptance thresholds: TBD
- `features/features.yml` is a TODO placeholder

## 8. Links

- [README.md](README.md) · [AGENTS.md](AGENTS.md) · [SCHEMA.md](SCHEMA.md) · [CHANGELOG.md](CHANGELOG.md)
