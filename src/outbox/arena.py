"""Tick-based arena: agents act, relay delivers, observer never synthesizes mid-race."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .agents import BaseAgent, build_default_agents
from .mailbox import Mailbox, Relay, append_jsonl, write_json
from .protocol import Message
from .score import score_trial


AGENT_IDS = ("scout", "claimant-a", "claimant-b")


class Arena:
    def __init__(self, root: Path, task: dict[str, Any], agents: dict[str, BaseAgent] | None = None) -> None:
        self.root = Path(root)
        self.task = task
        self.agents = agents or build_default_agents(lag_b=int(task.get("lag_b", 2)))
        self.boxes = {aid: Mailbox(self.root, aid) for aid in AGENT_IDS}
        self.relay = Relay(self.root, AGENT_IDS)
        self.tick = 0
        write_json(self.root / "task.json", task)

    def boot(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        open_msg = Message(
            type="task_open",
            from_agent="arena",
            to=list(AGENT_IDS) + ["ledger"],
            task_id=self.task["id"],
            env_seq=0,
            created_at_tick=0,
            closes_at_tick=int(self.task["closes_at_tick"]),
            payload={"question": self.task["question"], "family": self.task.get("family")},
        )
        for aid in AGENT_IDS:
            write_json(self.boxes[aid].inbox / f"000000_task_open_{open_msg.id}.json", open_msg.to_dict())
        append_jsonl(self.root / "ledger.jsonl", {"event": "deliver", **open_msg.to_dict()})

    def drain_inbox(self, agent_id: str) -> list[Message]:
        box = self.boxes[agent_id]
        lag = self.agents[agent_id].config.inbox_lag_ticks
        visible: list[Message] = []
        for path, msg in box.pending_inbox():
            if msg.created_at_tick > self.tick - lag:
                continue
            visible.append(msg)
            box.archive_message(path)
        return visible

    def commit(self, agent_id: str, messages: list[Message]) -> None:
        box = self.boxes[agent_id]
        state = box.load_state()
        for msg in messages:
            box.write_outbox(msg)
            state["seen_env_seq"] = max(int(state.get("seen_env_seq") or 0), msg.env_seq)
        box.save_state(state)
        self.relay.deliver_new()

    def run(self) -> dict[str, Any]:
        self.boot()
        close = int(self.task["closes_at_tick"])
        for tick in range(0, close + 1):
            self.tick = tick
            append_jsonl(self.root / "ledger.jsonl", {"event": "tick", "tick": tick, "task_id": self.task["id"]})
            order = ["scout", "claimant-a", "claimant-b"]
            for aid in order:
                incoming = self.drain_inbox(aid)
                self.agents[aid].ingest(incoming, tick)
                outgoing = self.agents[aid].step(tick, self.task)
                self.commit(aid, outgoing)
        result = score_trial(self.root, self.task)
        write_json(self.root / "result.json", result)
        return result


def new_run_dir(base: Path, run_id: str) -> Path:
    path = Path(base) / "runs" / run_id
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    return path
