# Agent guide — bamr87/outbox

This repository is a standalone harness, not the dash control plane.

Read `README.md` and `SCHEMA.md` before changing layout.

Do not add a central synthesizer that runs *during* a trial. Scoring happens after `closes_at_tick`.

Do not let one agent read another agent's `state.json`. Cross-agent writes go through outbox → relay → inbox.

Markdown prose in this repo is one paragraph per line.

## Verify

```bash
python3 -m pip install -e .
python3 -m unittest discover -s tests -v
python3 -m outbox.cli suite --tasks tasks --out /tmp/outbox-suite
```

No package pins. Python 3.11+ is enough. The offline policy agents must keep the suite green without an xAI key.

## Governance

Read [`docs/constitution.md`](docs/constitution.md) before changing anything: it is the highest-precedence rule set here and wins over this file when they disagree. It changes only through a numbered record in [`docs/amendments/`](docs/amendments/), ratified by the maintainer merging the PR; agents propose, never merge. Every user-facing feature has a stable id in [`features/features.yml`](features/features.yml) with `status`, location and the PRD requirement ids it serves; the PRD's "Feature catalog" block is generated from it (`python3 <bamr87/bamr87>/tools/governance_lint.py prd-sync . --write`; check with `… check .`). Never hand-edit that block or `CHANGELOG.md`.
