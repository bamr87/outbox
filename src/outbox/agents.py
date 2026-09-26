"""Policy agents that stand in for three Grok bots when no API key is set.

They are intentionally asymmetric:
- scout publishes environment facts on a schedule and never claims
- claimant-a is fast and overconfident on whatever it has already seen
- claimant-b is delayed (inbox lag) and more conservative, but updates after new env
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .protocol import Message


@dataclass
class AgentConfig:
    agent_id: str
    role: str
    inbox_lag_ticks: int = 0
    temperature: float = 0.2


class BaseAgent:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.seen_env: list[Message] = []
        self.my_claims: list[Message] = []
        self.known_peer_claims: list[Message] = []

    def ingest(self, messages: list[Message], tick: int) -> None:
        for msg in messages:
            if msg.type == "env_event":
                if all(e.id != msg.id for e in self.seen_env):
                    self.seen_env.append(msg)
            elif msg.type == "claim" and msg.from_agent != self.config.agent_id:
                self.known_peer_claims.append(msg)
            elif msg.type == "retract":
                self.known_peer_claims = [c for c in self.known_peer_claims if c.id != msg.parent_id]

    def latest_env(self) -> Message | None:
        if not self.seen_env:
            return None
        return max(self.seen_env, key=lambda m: m.env_seq)

    def step(self, tick: int, task: dict[str, Any]) -> list[Message]:
        raise NotImplementedError


class Scout(BaseAgent):
    def step(self, tick: int, task: dict[str, Any]) -> list[Message]:
        schedule = task.get("env_schedule") or []
        out: list[Message] = []
        published = {m.env_seq for m in self.seen_env}
        for event in schedule:
            if int(event["tick"]) != tick:
                continue
            seq = int(event["env_seq"])
            if seq in published:
                continue
            msg = Message(
                type="env_event",
                from_agent="scout",
                to=["claimant-a", "claimant-b", "ledger"],
                task_id=task["id"],
                env_seq=seq,
                created_at_tick=tick,
                payload={"fact": event["fact"], "answer_hint": event.get("answer_hint")},
                note=event.get("note", ""),
            )
            out.append(msg)
            self.seen_env.append(msg)
        if tick == int(task.get("deadline_ping_tick", -1)):
            out.append(
                Message(
                    type="deadline_ping",
                    from_agent="scout",
                    to=["claimant-a", "claimant-b", "ledger"],
                    task_id=task["id"],
                    env_seq=(self.latest_env().env_seq if self.latest_env() else 0),
                    created_at_tick=tick,
                    closes_at_tick=int(task["closes_at_tick"]),
                    note="window closing",
                )
            )
        return out


class ClaimantA(BaseAgent):
    """Fast, high confidence, no patience for later facts."""

    def step(self, tick: int, task: dict[str, Any]) -> list[Message]:
        env = self.latest_env()
        if env is None:
            return []
        sticky = bool(task.get("a_sticky"))
        if sticky and self.my_claims:
            return []
        answer = (env.payload or {}).get("answer_hint") or (env.payload or {}).get("fact")
        if self.my_claims and self.my_claims[-1].payload.get("answer") == answer:
            return []
        out: list[Message] = []
        if self.my_claims and self.my_claims[-1].payload.get("answer") != answer:
            prev = self.my_claims[-1]
            out.append(
                Message(
                    type="retract",
                    from_agent="claimant-a",
                    to=["claimant-b", "ledger"],
                    task_id=task["id"],
                    env_seq=env.env_seq,
                    created_at_tick=tick,
                    parent_id=prev.id,
                    note="stale answer",
                )
            )
        claim = Message(
            type="claim",
            from_agent="claimant-a",
            to=["claimant-b", "ledger"],
            task_id=task["id"],
            env_seq=env.env_seq,
            created_at_tick=tick,
            confidence=round(min(0.93, 0.72 + 0.07 * env.env_seq), 2),
            payload={"answer": answer, "evidence": [env.payload.get("fact")]},
            note="fast snapshot",
        )
        self.my_claims.append(claim)
        out.append(claim)
        return out


class ClaimantB(BaseAgent):
    """Slow inbox. Waits one extra tick after a new env before claiming. Lower confidence."""

    def __init__(self, config: AgentConfig) -> None:
        super().__init__(config)
        self._ready_from_tick: dict[int, int] = {}

    def step(self, tick: int, task: dict[str, Any]) -> list[Message]:
        env = self.latest_env()
        if env is None:
            return []
        if env.env_seq not in self._ready_from_tick:
            self._ready_from_tick[env.env_seq] = tick + 1
        if tick < self._ready_from_tick[env.env_seq]:
            return []
        answer = (env.payload or {}).get("answer_hint") or (env.payload or {}).get("fact")
        if self.my_claims and self.my_claims[-1].payload.get("answer") == answer:
            return []
        out: list[Message] = []
        if self.my_claims and self.my_claims[-1].payload.get("answer") != answer:
            prev = self.my_claims[-1]
            out.append(
                Message(
                    type="retract",
                    from_agent="claimant-b",
                    to=["claimant-a", "ledger"],
                    task_id=task["id"],
                    env_seq=env.env_seq,
                    created_at_tick=tick,
                    parent_id=prev.id,
                    note="caught up",
                )
            )
        claim = Message(
            type="claim",
            from_agent="claimant-b",
            to=["claimant-a", "ledger"],
            task_id=task["id"],
            env_seq=env.env_seq,
            created_at_tick=tick,
            confidence=round(min(0.84, 0.55 + 0.06 * env.env_seq), 2),
            payload={"answer": answer, "evidence": [env.payload.get("fact")]},
            note="lagged but current",
        )
        self.my_claims.append(claim)
        out.append(claim)
        return out


def build_default_agents(lag_b: int = 2) -> dict[str, BaseAgent]:
    return {
        "scout": Scout(AgentConfig("scout", "environment", inbox_lag_ticks=0)),
        "claimant-a": ClaimantA(AgentConfig("claimant-a", "fast", inbox_lag_ticks=0, temperature=0.9)),
        "claimant-b": ClaimantB(AgentConfig("claimant-b", "slow", inbox_lag_ticks=lag_b, temperature=0.2)),
    }
