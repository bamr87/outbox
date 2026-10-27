"""Belt profile: stage-to-stage handoffs as immutable outbox messages.

The arena races three agents on one task. The belt carries one piece of work
between pipeline stations (scout -> planner -> drafter -> verifier -> publisher
-> learner). Both keep the same rule: a sender writes an immutable file into its
own outbox, and only the relay copies it into recipient inboxes and the ledger.

The relay is the single writer of ``ledger.jsonl`` and the only place a sequence
number is minted, so concurrent senders never race on shared state. The ledger
is the relay's memory: what it has delivered and the last sequence number are
read back from it, so a crash between an inbox copy and the ledger append is
repaired by re-running the relay.
"""

from __future__ import annotations

import fcntl
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .mailbox import append_jsonl, write_json
from .score import load_jsonl

PROFILE = "belt"
SCHEMA_VERSION = 1
KINDS = ("idea", "assignment", "draft", "verdict", "approval", "published", "signal")
NEEDS_PARENT = frozenset({"assignment", "draft", "verdict", "approval", "published"})
_TRACE = re.compile(r"\A[0-9a-f]{32}\Z")
_STATION = re.compile(r"\A[a-z0-9][a-z0-9_-]*\Z")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class Handoff:
    kind: str
    from_station: str
    to: list[str]
    subject: str
    payload: dict[str, Any] = field(default_factory=dict)
    parent_id: str | None = None
    trace: str = field(default_factory=lambda: uuid4().hex)
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(default_factory=utc_now)
    ref: dict[str, Any] | None = None

    def reply(self, kind: str, from_station: str, to: list[str], payload: dict[str, Any] | None = None, **extra: Any) -> "Handoff":
        """A follow-on message for the same piece of work: same subject and trace, parented to this one."""
        return Handoff(
            kind=kind,
            from_station=from_station,
            to=to,
            subject=extra.pop("subject", self.subject),
            payload=payload or {},
            parent_id=self.id,
            trace=self.trace,
            **extra,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": PROFILE,
            "schema_version": SCHEMA_VERSION,
            "type": "handoff",
            "id": self.id,
            "from": self.from_station,
            "to": list(self.to),
            "kind": self.kind,
            "subject": self.subject,
            "trace": self.trace,
            "parent_id": self.parent_id,
            "created_at": self.created_at,
            "payload": self.payload,
            "ref": self.ref,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Handoff":
        return cls(
            kind=data["kind"],
            from_station=data["from"],
            to=list(data["to"]),
            subject=data["subject"],
            payload=dict(data.get("payload") or {}),
            parent_id=data.get("parent_id"),
            trace=data["trace"],
            id=data["id"],
            created_at=data["created_at"],
            ref=data.get("ref"),
        )


def validate_handoff(data: dict[str, Any]) -> list[str]:
    """The stdlib twin of protocol/belt.schema.json."""
    errors: list[str] = []
    if data.get("profile") != PROFILE:
        errors.append("profile_must_be_belt")
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append("bad_schema_version")
    if data.get("type") != "handoff":
        errors.append("type_must_be_handoff")
    for key in ("id", "from", "subject", "created_at"):
        if not isinstance(data.get(key), str) or not data.get(key):
            errors.append(f"missing:{key}")
    if isinstance(data.get("from"), str) and data.get("from") and not _STATION.match(data["from"]):
        errors.append("bad_station_name")
    to = data.get("to")
    if not isinstance(to, list) or not to or not all(isinstance(t, str) and t for t in to):
        errors.append("to_must_be_nonempty_list")
    kind = data.get("kind")
    if kind not in KINDS:
        errors.append(f"bad_kind:{kind}")
    if kind in NEEDS_PARENT and not data.get("parent_id"):
        errors.append(f"{kind}_needs_parent_id")
    if not isinstance(data.get("trace"), str) or not _TRACE.match(data["trace"]):
        errors.append("trace_must_be_32_hex")
    if not isinstance(data.get("payload"), dict):
        errors.append("payload_must_be_object")
    ref = data.get("ref")
    if ref is not None and not isinstance(ref, dict):
        errors.append("ref_must_be_object_or_null")
    return errors


def to_cloudevent(data: dict[str, Any]) -> dict[str, Any]:
    """Map a belt message onto a CloudEvents 1.0 envelope (the binding zer0-cms's stream uses).

    The belt trace becomes the W3C trace id and the message id's first 16 hex
    digits become the span id, so a piece's whole journey joins one trace.
    """
    span = data["id"].replace("-", "")[:16]
    return {
        "specversion": "1.0",
        "id": data["id"],
        "source": f"outbox://belt/{data['from']}",
        "type": f"dev.bamr87.outbox.{data['kind']}",
        "subject": data["subject"],
        "time": data["created_at"],
        "datacontenttype": "application/json",
        "traceparent": f"00-{data['trace']}-{span}-01",
        "data": {"to": data["to"], "parent_id": data.get("parent_id"), "payload": data["payload"], "ref": data.get("ref")},
    }


class Station:
    """One stage's private mailbox. Peers write only through the relay."""

    def __init__(self, root: Path, station_id: str) -> None:
        if not _STATION.match(station_id):
            raise ValueError(f"bad station name: {station_id!r}")
        self.station_id = station_id
        self.root = Path(root) / "stations" / station_id
        self.inbox = self.root / "inbox"
        self.archive = self.inbox / "archive"
        self.outbox = self.root / "outbox"
        for folder in (self.inbox, self.archive, self.outbox):
            folder.mkdir(parents=True, exist_ok=True)

    def send(self, message: Handoff) -> Path:
        if message.from_station != self.station_id:
            raise ValueError(f"{self.station_id} cannot send as {message.from_station}")
        data = message.to_dict()
        errors = validate_handoff(data)
        if errors:
            raise ValueError(f"invalid handoff {message.id}: {errors}")
        stamp = re.sub(r"[^0-9]", "", message.created_at)
        path = self.outbox / f"{stamp}_{message.kind}_{message.id}.json"
        if path.exists():
            raise FileExistsError(f"outbox records are immutable: {path.name}")
        write_json(path, data)
        return path

    def receive(self, kinds: tuple[str, ...] | None = None) -> list[Handoff]:
        """Take delivered messages in relay order and archive them."""
        taken: list[Handoff] = []
        for path in sorted(self.inbox.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if kinds and data.get("kind") not in kinds:
                continue
            taken.append(Handoff.from_dict(data))
            path.replace(self.archive / path.name)
        return taken


class BeltRelay:
    """Copies committed outbox files into recipient inboxes and the ledger, in one total order."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.ledger = self.root / "ledger.jsonl"
        self.dead_letter = self.root / "dead_letter.jsonl"
        self.lock_path = self.root / ".relay.lock"
        (self.root / "stations").mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        with self.lock_path.open("a") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def stations(self) -> list[str]:
        return sorted(p.name for p in (self.root / "stations").iterdir() if p.is_dir())

    def _pending(self) -> list[tuple[str, str, Path]]:
        found: list[tuple[str, str, Path]] = []
        for station in self.stations():
            for path in (self.root / "stations" / station / "outbox").glob("*.json"):
                found.append((path.name.split("_", 1)[0], station, path))
        return sorted(found)

    def deliver_new(self) -> list[dict[str, Any]]:
        delivered: list[dict[str, Any]] = []
        with self._exclusive():
            rows = load_jsonl(self.ledger)
            seen = {r["id"] for r in rows if r.get("id")}
            seen |= {r["id"] for r in load_jsonl(self.dead_letter) if r.get("id")}
            seq = max((int(r.get("seq") or 0) for r in rows), default=0)
            known = set(self.stations())
            for _stamp, station, path in self._pending():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    data = {"id": f"unparseable:{station}/{path.name}", "_error": str(exc)}
                msg_id = data.get("id") or f"no-id:{station}/{path.name}"
                if msg_id in seen:
                    continue
                errors = validate_handoff(data) if "_error" not in data else ["unparseable_json"]
                if data.get("from") != station:
                    errors.append(f"from_mismatch:{data.get('from')}!={station}")
                unknown = [d for d in data.get("to") or [] if d != "ledger" and d not in known]
                errors.extend(f"unknown_recipient:{d}" for d in unknown)
                if errors:
                    append_jsonl(self.dead_letter, {"id": msg_id, "station": station, "path": path.name, "errors": errors})
                    seen.add(msg_id)
                    continue
                seq += 1
                name = f"{seq:08d}_{data['kind']}_{msg_id}.json"
                for dest in data["to"]:
                    if dest != "ledger":
                        write_json(self.root / "stations" / dest / "inbox" / name, data)
                record = {"seq": seq, "event": "deliver", **data}
                append_jsonl(self.ledger, record)
                seen.add(msg_id)
                delivered.append(record)
        return delivered


class Belt:
    """A set of stations sharing one relay and one ledger."""

    def __init__(self, root: Path, stations: list[str] | tuple[str, ...] = ()) -> None:
        self.root = Path(root)
        self.relay = BeltRelay(self.root)
        self._stations = {name: Station(self.root, name) for name in stations}

    def station(self, name: str) -> Station:
        if name not in self._stations:
            self._stations[name] = Station(self.root, name)
        return self._stations[name]

    def deliver(self) -> list[dict[str, Any]]:
        return self.relay.deliver_new()

    def ledger(self) -> list[dict[str, Any]]:
        return load_jsonl(self.relay.ledger)

    def dead_letters(self) -> list[dict[str, Any]]:
        return load_jsonl(self.relay.dead_letter)

    def journey(self, trace: str) -> list[dict[str, Any]]:
        """Every delivered message for one piece of work, in relay order."""
        return [row for row in self.ledger() if row.get("trace") == trace]


def write_belt_summary(belt: Belt, path: Path) -> dict[str, Any]:
    rows = belt.ledger()
    seqs = [int(r["seq"]) for r in rows]
    summary = {
        "delivered": len(rows),
        "dead_letters": len(belt.dead_letters()),
        "traces": len({r["trace"] for r in rows}),
        "kinds": {k: sum(1 for r in rows if r["kind"] == k) for k in KINDS if any(r["kind"] == k for r in rows)},
        "seq_gapless": seqs == list(range(1, len(seqs) + 1)),
        "ids_unique": len({r["id"] for r in rows}) == len(rows),
    }
    write_json(path, summary)
    return summary
