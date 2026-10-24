<!-- kit: sdlc v0.1.0 · AGENTS.md is the CANONICAL agent instructions file (decision D4, UPS-AGENT-07/08). -->

# outbox

> How to work in outbox, the three-agent competitive outbox harness: the exact commands, the layout, the trial and relay rules the code enforces, and the conventions.

## What this repo is

outbox is a standalone, measurable harness for competitive asynchronous messaging among three agents — a scout and two claimants. Agents publish only by writing immutable outbox records, a relay copies them into the recipients' inboxes and the ledger, environment lag is injected, the claimants race on confidence, and a replay scorer judges who was right once the trial closes. Offline policy bots stand in for live Grok agents, so the suite needs no network and no key. It serves researchers and engineers evaluating multi-agent coordination and calibration; it is a standalone harness, not the dash control plane. A change is done when the unit tests and the suite smoke pass offline, the generated docs are current, and `README.md` and `SCHEMA.md` reflect any layout change.

## Stack & commands

Python 3.11+, standard library only: no runtime dependencies and no package pins.

```bash
python3 -m pip install -e .                                          # install (Python 3.11+, stdlib only)
python3 -m outbox.cli run --task tasks/stale_fact.json --out .       # one trial -> runs/<run-id>/
python3 -m outbox.cli watch --ledger runs/*/ledger.jsonl             # replay that trial's watch column
python3 -m outbox.cli suite --tasks tasks --out /tmp/outbox-suite    # every task family, summarized
python3 -m unittest discover -s tests -v                             # the offline test suite
```

```bash
python3 -m outbox.cli init --task tasks/stale_fact.json --arena /tmp/outbox-arena   # mailbox dirs + task_open only
python3 -m outbox.cli score --arena runs/<run-id>                    # re-score a finished arena
python3 tools/gen_cli_reference.py --write                           # regenerate docs/reference/cli.md
python3 tools/docs_index.py --write                                  # regenerate the docs/README.md index
```

Every subcommand and flag is in [docs/reference/cli.md](docs/reference/cli.md), generated from `outbox.cli.build_parser()`. CI (`.github/workflows/test.yml`) runs the install, the unit tests and the suite smoke; `ci.yml` calls the fleet's shared gate.

## Layout

[`SCHEMA.md`](SCHEMA.md) is the placement contract: what goes in each top-level directory and what is forbidden there. In short: `src/outbox/` (protocol, mailbox, arena, policy agents, scorer, CLI), `tasks/` (frozen task cards the tests use), `agents/` (system cards and the Grok Bots kickoff), `protocol/schema.json` (the message schema), `tests/` (unittest), `tools/` (doc generators), `docs/` (the index, ADRs, the generated CLI reference), and `runs/` (local trial output, gitignored).

## Conventions

- **Backlog of record:** GitHub Issues with the fleet labels (`.github/sdlc.yml`, issues mode). Pick up issues labelled `agent:ready`; never touch `agent:hold` or `human-review`.
- **Definition of Done:** the fleet default pull request checklist (this repo has no `.github/pull_request_template.md`), plus the commands above passing offline.
- **Decisions:** ADRs in `docs/adr/NNNN-slug.md`, indexed in `docs/adr/README.md`. Add one for anything hard to reverse, such as a protocol or scoring change.
- **Commits:** Conventional Commits (`feat fix docs style refactor test chore perf ci build`). The PR title is the squash subject.
- **Branches:** `feature/`, `fix/`, `docs/`, `refactor/`, `test/` or `chore/` off `main`.
- **Changelog:** `CHANGELOG.md` is written by release-please (`.github/workflows/release.yml`) from the PR titles. Never edit it, or a version number, by hand.
- **Placement:** read `README.md` and `SCHEMA.md` before changing layout; a new directory needs a row in `SCHEMA.md` in the same change.
- **Generated files — never hand-edit:** `docs/reference/cli.md` (`tools/gen_cli_reference.py`) and the `AUTO:docs-index` span in `docs/README.md` (`tools/docs_index.py`, vendored byte-identical from the hub; change it there). `tests/test_docs.py` fails when either is stale.
- **Markdown:** one paragraph per line (no hard-wrapped prose); every document opens with one H1 and a one-sentence `>` summary.
- **Never:** push to `main` directly, commit lockfiles, secrets or trial output, add package pins, suppress type errors, or leave empty exception handlers.

## Trial and relay rules

These are the harness's invariants; the code and tests enforce them, and a change that bends one is a protocol change that needs an ADR.

- No central synthesizer runs *during* a trial. The observer only watches; scoring happens after `closes_at_tick`, against the task's judge field `expected_answer`.
- No agent reads another agent's `state.json`. The only cross-agent write is an immutable message: into the sender's outbox, then copied by the relay into each recipient inbox and `ledger.jsonl` (outbox → relay → inbox). Peers learn only from their inbox.
- Messages follow `protocol/schema.json`. `protocol.validate_message` rejects a bad message at write time, and the relay sends an invalid outbox file to `dead_letter.jsonl` instead of any inbox.
- The winner at close is the highest-confidence non-retracted claim whose `env_seq` is current; the task's `max_env_lag` may relax that.
- The offline policy agents (`src/outbox/agents.py`) must keep the unit tests and `suite` green without an xAI key; tests make no network calls.
- Task cards in `tasks/` are frozen fixtures the tests use. Widen or close a race with `--lag-b`, or with a new card's `lag_b`, `a_sticky` and `env_schedule`.

## Fleet context

Member of the bamr87 fleet, vendored in the hub as the `projects/outbox` submodule: commit and push here first; the hub bumps its pointer afterwards. The standards live in the hub: <https://github.com/bamr87/bamr87/tree/main/specs> (the Universal Project Standard); this repo's SDLC declaration is `.github/sdlc.yml`. Loops that act on this repo: `claude.yml` (the `@claude` mention handler), `conformance.yml` (the hub's fleet conformance check), `verify.yml` (the fleet verify gate over `features/` and `verify/`), `release.yml` (release-please), and from the hub the daily fleet-pulse doctor (draft fix PRs for failing workflows) and the issue pipeline (`agent:ready` issues to draft PRs). None of them merges.

## Standard deviations

none

## Governance

Read [`docs/constitution.md`](docs/constitution.md) before changing anything: it is the highest-precedence rule set here and wins over this file when they disagree. It changes only through a numbered record in [`docs/amendments/`](docs/amendments/), ratified by the maintainer merging the PR; agents propose, never merge. Every user-facing feature has a stable id in [`features/features.yml`](features/features.yml) with `status`, location and the PRD requirement ids it serves; the PRD's "Feature catalog" block is generated from it (`python3 <bamr87/bamr87>/tools/governance_lint.py prd-sync . --write`; check with `… check .`). Never hand-edit that block or `CHANGELOG.md`.
