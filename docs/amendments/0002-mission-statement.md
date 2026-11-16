<!-- kit: governance v0.1.0 -->
# 0002 — Missions

- **Status:** proposed
- **Date:** 2026-11-16
- **Proposed by:** Grok Bot (agent), at the maintainer's request
- **Ratified by:**
- **Affects:** constitution §1; PRD §1.1; feature OUTBOX-009
- **Supersedes:** none
- **Related:** [PRD.md](../../PRD.md) §1.1; feature OUTBOX-009

## Context

Constitution §1 Mission was a product description (or, for a new constitution, had not been written yet), not a three-mission direction statement. The fleet now requires every repo's constitution §1 to be one direction statement followed by exactly three numbered missions, adopted through a numbered amendment the maintainer ratifies by merging. The maintainer asked for those missions to be comprehensive and epic: Outbox as the ultimate open home for all things competitive agent messaging. Mission 1 follows the maintainer's wording, adapted to this domain: provide data, tools, resources and transparency for all stakeholders, domain practitioners and the general public. The ambition is the direction, not a claim that the work is already finished. The missions are aligned with this repo's existing constitution and PRD.

## Amendment

Replace constitution §1 with the following, retitled **Missions**:

Outbox's direction is to become the ultimate open home for all things competitive agent messaging — data, tools, resources and transparency, gathered in one place that anyone can inspect and reuse.

1. **Provide data, tools, resources and transparency for all stakeholders, multi-agent researchers and practitioners, and the general public.** Analysts, harness authors, agent builders and anyone curious about how agents argue in the open are first-class audiences. The aim is a shared, inspectable commons: the same protocol, the same traces, and the same scoring rules for everyone.
2. **Make every outbound message immutable, every peer exchange inbox-only, and every race replayable and auditable.** Agents publish only by writing immutable outbox records; peers never mutate each other's memory. A filesystem inbox and outbox, injected environment lag, a confidence race and a replay scorer judge who published what, when, with what confidence, and whether they were right.
3. **Expose failure modes in the open — stale facts, confidence inversion, lag-window misses — through a reproducible offline harness and a strict message protocol anyone can run and inspect.** Policy bots stand in for live agents so the suite needs no network and no key, and the protocol itself remains the contract.

These missions set the course; they do not claim the work is finished. They serve the PRD goals: a reproducible offline suite, visible failure modes, and a strict message protocol.

_Source: [PRD.md](../PRD.md) §1.1._


## Consequences

Agents and reviewers judge fit against these three missions before inventing scope, treating them as the direction of the repo rather than a description of what already ships. The same text is added as PRD §1.1 so the constitution's `_Source:` line stays accurate. The original PRD purpose / overview paragraph is left in place. This PR also aligns the README and AGENTS.md taglines with the missions and links them to constitution §1, and adds feature OUTBOX-009 to the feature catalog, traced to requirement MR1. Merging this PR ratifies amendment 0002. Until then the record stays `Status: proposed`.
