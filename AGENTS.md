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
