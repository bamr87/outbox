import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path

from outbox.belt import KINDS, Belt, BeltRelay, Handoff, Station, to_cloudevent, validate_handoff
from outbox.mailbox import write_json

ROOT = Path(__file__).resolve().parents[1]
STATIONS = ("scout", "planner", "drafter", "verifier")


def _send_many(root: str, station: str, count: int) -> None:
    box = Station(Path(root), station)
    for n in range(count):
        box.send(Handoff(kind="idea", from_station=station, to=["planner", "ledger"], subject=f"{station}-{n}"))


def _relay_loop(root: str, rounds: int) -> None:
    relay = BeltRelay(Path(root))
    for _ in range(rounds):
        relay.deliver_new()


class BeltTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.belt = Belt(self.root, STATIONS)

    def tearDown(self):
        self._tmp.cleanup()

    def test_handoff_reaches_inbox_and_ledger_in_one_trace(self):
        idea = Handoff(kind="idea", from_station="scout", to=["planner", "ledger"], subject="fp-1", payload={"title": "x"})
        self.belt.station("scout").send(idea)
        self.belt.deliver()
        taken = self.belt.station("planner").receive()
        self.assertEqual([m.id for m in taken], [idea.id])
        assignment = taken[0].reply("assignment", "planner", ["drafter", "ledger"], {"backlog_id": "SRC-001"})
        self.belt.station("planner").send(assignment)
        self.belt.deliver()
        journey = self.belt.journey(idea.trace)
        self.assertEqual([r["kind"] for r in journey], ["idea", "assignment"])
        self.assertEqual([r["seq"] for r in journey], [1, 2])
        self.assertEqual(journey[1]["parent_id"], idea.id)
        self.assertEqual(list(self.belt.station("planner").inbox.glob("*.json")), [])

    def test_outbox_records_are_immutable(self):
        msg = Handoff(kind="idea", from_station="scout", to=["planner"], subject="fp")
        self.belt.station("scout").send(msg)
        with self.assertRaises(FileExistsError):
            self.belt.station("scout").send(msg)

    def test_a_station_cannot_send_as_another(self):
        with self.assertRaises(ValueError):
            self.belt.station("scout").send(Handoff(kind="idea", from_station="planner", to=["drafter"], subject="s"))

    def test_forged_unknown_and_invalid_messages_go_to_dead_letter(self):
        forged = Handoff(kind="idea", from_station="planner", to=["drafter"], subject="s").to_dict()
        write_json(self.root / "stations" / "scout" / "outbox" / "1_idea_forged.json", forged)
        lost = Handoff(kind="idea", from_station="scout", to=["nobody"], subject="s").to_dict()
        write_json(self.root / "stations" / "scout" / "outbox" / "2_idea_lost.json", lost)
        orphan = Handoff(kind="draft", from_station="scout", to=["verifier"], subject="s").to_dict()
        write_json(self.root / "stations" / "scout" / "outbox" / "3_draft_orphan.json", orphan)
        (self.root / "stations" / "scout" / "outbox" / "4_idea_junk.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.belt.deliver(), [])
        reasons = {row["path"]: row["errors"] for row in self.belt.dead_letters()}
        self.assertTrue(any(e.startswith("from_mismatch") for e in reasons["1_idea_forged.json"]))
        self.assertIn("unknown_recipient:nobody", reasons["2_idea_lost.json"])
        self.assertIn("draft_needs_parent_id", reasons["3_draft_orphan.json"])
        self.assertIn("unparseable_json", reasons["4_idea_junk.json"])
        self.assertEqual(list(self.belt.station("drafter").inbox.glob("*.json")), [])
        self.assertEqual(self.belt.deliver(), [])

    def test_relay_is_idempotent_and_repairs_a_crash_before_the_ledger_append(self):
        msg = Handoff(kind="idea", from_station="scout", to=["planner", "ledger"], subject="fp")
        self.belt.station("scout").send(msg)
        # Simulate a crash after the inbox copy but before the ledger append.
        write_json(self.root / "stations" / "planner" / "inbox" / f"00000001_idea_{msg.id}.json", msg.to_dict())
        delivered = self.belt.deliver()
        self.assertEqual(len(delivered), 1)
        self.assertEqual(self.belt.deliver(), [])
        self.assertEqual(len(list(self.belt.station("planner").inbox.glob("*.json"))), 1)
        self.assertEqual(len(self.belt.ledger()), 1)

    def test_concurrent_senders_and_relays_never_duplicate_or_lose_a_message(self):
        ctx = multiprocessing.get_context("spawn")
        senders = [ctx.Process(target=_send_many, args=(str(self.root), s, 25)) for s in ("scout", "drafter", "verifier")]
        relays = [ctx.Process(target=_relay_loop, args=(str(self.root), 30)) for _ in range(3)]
        for proc in senders + relays:
            proc.start()
        for proc in senders + relays:
            proc.join(60)
            self.assertEqual(proc.exitcode, 0)
        self.belt.deliver()
        rows = self.belt.ledger()
        self.assertEqual(len(rows), 75)
        self.assertEqual(len({r["id"] for r in rows}), 75)
        self.assertEqual([r["seq"] for r in rows], list(range(1, 76)))
        self.assertEqual(len(list(self.belt.station("planner").inbox.glob("*.json"))), 75)
        self.assertEqual(self.belt.dead_letters(), [])

    def test_cloudevent_binding_carries_the_trace(self):
        msg = Handoff(kind="published", from_station="verifier", to=["ledger"], subject="fp", parent_id="p1")
        event = to_cloudevent(msg.to_dict())
        self.assertEqual(event["specversion"], "1.0")
        self.assertEqual(event["type"], "dev.bamr87.outbox.published")
        self.assertRegex(event["traceparent"], r"^00-[0-9a-f]{32}-[0-9a-f]{16}-01$")
        self.assertIn(msg.trace, event["traceparent"])

    def test_validator_matches_the_json_schema(self):
        schema = json.loads((ROOT / "protocol" / "belt.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(tuple(schema["properties"]["kind"]["enum"]), KINDS)
        sample = Handoff(kind="idea", from_station="scout", to=["planner"], subject="s").to_dict()
        self.assertTrue(set(schema["required"]) <= set(sample))
        self.assertEqual(validate_handoff(sample), [])
        for key in schema["required"]:
            broken = {k: v for k, v in sample.items() if k != key}
            self.assertNotEqual(validate_handoff(broken), [], key)


if __name__ == "__main__":
    unittest.main()
