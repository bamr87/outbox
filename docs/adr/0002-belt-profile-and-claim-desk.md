# ADR 0002 — Belt profile and claim desk

- **Status**: proposed
- **Date**: 2026-10-26
- **Deciders**: maintainer (to ratify by merging); drafted by an AI agent
- **Related**: the Outbox-Lifehack platform plan (phases 2 and 3), `e2e/run.py`

## Context

The platform plan joins outbox, zer0-cms, lifehacker.dev, gitorio and scripts into one content pipeline: Sense, Plan, Produce, Verify, Publish, Learn. In lifehacker.dev today those stages coordinate through one shared file (`_data/backlog.yml`), staggered crons and labels. Two scout runs branched off the same `main` both mint the next `SRC-###` id; the repo's merge driver then reports a conflict, and under the older `union` strategy the file silently gained a duplicate id and doubled keys. The repo's own lint (`duplicate-backlog-id`) keeps catching it.

outbox already has the missing mechanism: a sender writes an immutable file into its own outbox, and only a relay copies it into inboxes and the ledger. It also has a scorer that judges a confidence race under lag, which matches the Wire desk's main risk, publishing a fact its source no longer supports. Both uses need small protocol and scoring changes, and AGENTS.md requires an ADR for those.

## Decision

1. **A second message profile, `belt`.** `protocol/belt.schema.json` defines a `handoff` message with a `kind` (`idea`, `assignment`, `draft`, `verdict`, `approval`, `published`, `signal`), a `subject` (the stable key of a piece of work), a `trace` (32 hex digits, shared by every message about that piece), a `parent_id` (required for kinds that answer another message) and an optional `ref` pointing at an artifact instead of carrying its body. `src/outbox/belt.py` implements stations, a relay and the validator. Arena messages keep `protocol/schema.json` unchanged; v1 messages stay valid.
2. **The relay is the single writer.** `BeltRelay` takes an exclusive lock, delivers pending outbox files in one total order, and mints a gapless `seq` for each. It rebuilds what it has delivered from `ledger.jsonl` on every run, so a crash between an inbox copy and the ledger append is repaired by running it again. A file whose `from` does not match the station it sits in, names an unknown recipient, or fails validation goes to `dead_letter.jsonl`.
3. **A CloudEvents binding.** `to_cloudevent` maps a belt message onto a CloudEvents 1.0 envelope, with the trace as the W3C trace id, so belt traffic can join zer0-cms's event stream later.
4. **Late-resolved truth.** A task may open with `expected_answer` unset. `score_trial` then reports `resolved: false` and leaves `correct` and `confidence_inversion` as `null` instead of scoring every claim wrong. A resolver re-scores the same ledger once the truth is known.
5. **The claim desk.** `src/outbox/claimdesk.py` turns each factual claim in a draft into a `source_claim` arena task. The scout replays what the claim's sources said over time, the two claimants race as they do today, and the existing winner rule decides at close. A claim passes only if the winner agrees with the draft, is current, and is at least 0.6 confident. Resolution produces Brier scores per verifier and per draft author.

## Consequences

**Positive**

- Stages never write shared state, so the id race cannot happen on the belt; `e2e/run.py` reproduces the race with lifehacker.dev's own code and shows the belt path passing its lint.
- One ledger traces a piece from idea to correction.
- Factual pieces get a pre-merge check against their sources and a calibration record after the fact.

**Negative**

- Two schemas to maintain instead of one.
- The relay's lock is `fcntl.flock`, so it is POSIX-only (macOS and Linux runners).
- A source that is wrong at close still passes the desk; only resolution and a post-publication recheck catch it.

## Alternatives considered

- **Extend `protocol/schema.json` with a `profile` field.** Rejected: the arena's invariants (no synthesis during a trial, frozen task cards) would have to carve out exceptions inside one schema.
- **Lock `_data/backlog.yml` instead.** Rejected: a lock serializes one workflow, not every writer across branches; the collision happens at merge time, not write time.
- **Score unresolved tasks as wrong.** Rejected: it reports false confidence inversions for every claim whose truth is not yet known.

## Supersedes / Superseded by

- Supersedes: none
- Superseded by: none
