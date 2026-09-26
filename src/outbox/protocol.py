"""Message contract for the competitive outbox.

Agents never write another agent's state. The only cross-agent write is an
immutable message: first into the sender outbox, then copied into each
recipient inbox (transactional outbox + relay).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal
from uuid import uuid4

MessageType = Literal["claim", "retract", "env_event", "deadline_ping", "task_open"]

REQUIRED = (
    "id",
    "from_agent",
    "to",
    "type",
    "task_id",
    "env_seq",
    "created_at_tick",
)


@dataclass
class Message:
    type: MessageType
    from_agent: str
    to: list[str]
    task_id: str
    env_seq: int
    created_at_tick: int
    id: str = field(default_factory=lambda: str(uuid4()))
    confidence: float | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    closes_at_tick: int | None = None
    parent_id: str | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["from"] = data.pop("from_agent")
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Message":
        payload = dict(data)
        if "from" in payload and "from_agent" not in payload:
            payload["from_agent"] = payload.pop("from")
        known = {k: payload.get(k) for k in cls.__dataclass_fields__}
        return cls(**known)  # type: ignore[arg-type]


def validate_message(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    body = dict(data)
    if "from" in body and "from_agent" not in body:
        body["from_agent"] = body["from"]
    for key in REQUIRED:
        if key not in body or body[key] in (None, "", []):
            errors.append(f"missing:{key}")
    msg_type = body.get("type")
    if msg_type not in {"claim", "retract", "env_event", "deadline_ping", "task_open"}:
        errors.append(f"bad_type:{msg_type}")
    if msg_type == "claim":
        conf = body.get("confidence")
        if not isinstance(conf, (int, float)) or not 0.0 <= float(conf) <= 1.0:
            errors.append("claim_needs_confidence_0_1")
        answer = (body.get("payload") or {}).get("answer")
        if answer in (None, ""):
            errors.append("claim_needs_payload.answer")
    if msg_type == "retract" and not body.get("parent_id"):
        errors.append("retract_needs_parent_id")
    dest = body.get("to")
    if dest is not None and not isinstance(dest, list):
        errors.append("to_must_be_list")
    return errors
