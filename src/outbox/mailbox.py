"""Filesystem inbox/outbox with atomic writes and a private state file."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .protocol import Message, validate_message


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_json(path: Path, data: Any) -> None:
    atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def append_jsonl(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(data, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


class Mailbox:
    """One agent's private directory. Peers must not read state.json."""

    def __init__(self, root: Path, agent_id: str) -> None:
        self.agent_id = agent_id
        self.root = Path(root) / "agents" / agent_id
        self.inbox = self.root / "inbox"
        self.archive = self.inbox / "archive"
        self.outbox = self.root / "outbox"
        self.state_path = self.root / "state.json"
        for folder in (self.inbox, self.archive, self.outbox):
            folder.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            write_json(self.state_path, {"agent": agent_id, "seen_env_seq": 0, "inbox_ids": []})

    def load_state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def save_state(self, state: dict[str, Any]) -> None:
        write_json(self.state_path, state)

    def pending_inbox(self) -> list[tuple[Path, Message]]:
        items: list[tuple[Path, Message]] = []
        for path in sorted(self.inbox.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            items.append((path, Message.from_dict(data)))
        return items

    def archive_message(self, path: Path) -> None:
        dest = self.archive / path.name
        path.replace(dest)

    def write_outbox(self, message: Message) -> Path:
        errors = validate_message(message.to_dict())
        if errors:
            raise ValueError(f"invalid message {message.id}: {errors}")
        path = self.outbox / f"{message.created_at_tick:06d}_{message.type}_{message.id}.json"
        write_json(path, message.to_dict())
        return path


class Relay:
    """Copies a committed outbox file into recipient inboxes and the ledger."""

    def __init__(self, arena_root: Path, agent_ids: Iterable[str]) -> None:
        self.root = Path(arena_root)
        self.agent_ids = list(agent_ids)
        self.ledger = self.root / "ledger.jsonl"
        self.seen_path = self.root / ".relay_seen.json"
        if not self.seen_path.exists():
            write_json(self.seen_path, {"ids": []})

    def seen_ids(self) -> set[str]:
        return set(json.loads(self.seen_path.read_text(encoding="utf-8")).get("ids", []))

    def deliver_new(self) -> list[str]:
        delivered: list[str] = []
        seen = self.seen_ids()
        for agent_id in self.agent_ids:
            outbox = self.root / "agents" / agent_id / "outbox"
            if not outbox.exists():
                continue
            for path in sorted(outbox.glob("*.json")):
                data = json.loads(path.read_text(encoding="utf-8"))
                msg_id = data.get("id")
                if not msg_id or msg_id in seen:
                    continue
                errors = validate_message(data)
                if errors:
                    append_jsonl(
                        self.root / "dead_letter.jsonl",
                        {"id": msg_id, "errors": errors, "path": str(path)},
                    )
                    seen.add(msg_id)
                    continue
                recipients = data.get("to") or []
                for dest in recipients:
                    if dest == "ledger":
                        continue
                    inbox = self.root / "agents" / dest / "inbox"
                    inbox.mkdir(parents=True, exist_ok=True)
                    write_json(inbox / path.name, data)
                append_jsonl(self.ledger, {"event": "deliver", **data})
                seen.add(msg_id)
                delivered.append(msg_id)
        write_json(self.seen_path, {"ids": sorted(seen)})
        return delivered
