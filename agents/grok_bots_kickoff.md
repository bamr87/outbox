# Grok Bots group kickoff

Create a group with three Bots. Paste the matching agent card into each Bot's instructions. Then send this once. Do not @ anyone after kickoff unless you are playing Scout / environment.

```
Shared task: see the task card pinned above.

Rules:
- You may not edit another bot's messages.
- To compete you MUST post an OUTBOX block (claim | retract | env_event | deadline_ping).
- Highest confidence at close wins unless a later claim cites a newer env_seq.
- If you learn you are stale, outbox a retract or a superseding claim.
- Do not negotiate in prose. Do not divide labor. This is a race, not a committee.

Scout: inject env_seq in order from the task card, one event per message.
Claimant-A and Claimant-B: wake from inbox only.
```

Human observation: you are the environment clock. Every 20 seconds post a new fact as Scout, or paste the next `env_schedule` row. Export the thread at close and drop claims into `runs/<id>/ledger.jsonl` if you want the scorer.
