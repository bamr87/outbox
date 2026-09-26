import json
import tempfile
import unittest
from pathlib import Path

from outbox.arena import Arena
from outbox.score import load_jsonl, summarize


def load_task(name: str) -> dict:
    here = Path(__file__).resolve().parents[1] / "tasks" / name
    return json.loads(here.read_text(encoding="utf-8"))


class ArenaTests(unittest.TestCase):
    def test_stale_fact_a_is_first_b_catches_up(self):
        task = load_task("stale_fact.json")
        with tempfile.TemporaryDirectory() as tmp:
            result = Arena(Path(tmp) / "run", task).run()
        self.assertEqual(result["first_claim_from"], "claimant-a")
        self.assertEqual(result["winner"]["answer"], "green")
        self.assertTrue(result["correct"])
        self.assertGreaterEqual(result["retracts"], 1)
        self.assertFalse(result["confidence_inversion"])
        self.assertGreaterEqual(result["claims_per_agent"].get("claimant-b", 0), 1)

    def test_ambiguous_sticky_a_can_invert(self):
        task = load_task("ambiguous.json")
        with tempfile.TemporaryDirectory() as tmp:
            result = Arena(Path(tmp) / "run", task).run()
        self.assertEqual(result["winner"]["from"], "claimant-a")
        self.assertEqual(result["winner"]["answer"], "alpha")
        self.assertTrue(result["confidence_inversion"])
        self.assertFalse(result["correct"])

    def test_deadline_b_may_miss_late_full_hash(self):
        task = load_task("deadline.json")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "run"
            result = Arena(root, task).run()
            rows = load_jsonl(root / "ledger.jsonl")
        claims_b = [r for r in rows if r.get("from") == "claimant-b" and r.get("type") == "claim"]
        self.assertTrue(result["winner"])
        self.assertTrue(all(c.get("payload", {}).get("answer") != "deadbeef" for c in claims_b))

    def test_no_peer_state_reads_in_ledger_events(self):
        task = load_task("stale_fact.json")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "run"
            Arena(root, task).run()
            for agent in ("scout", "claimant-a", "claimant-b"):
                self.assertTrue((root / "agents" / agent / "state.json").exists())
                self.assertTrue((root / "agents" / agent / "outbox").exists())

    def test_summarize_shape(self):
        summary = summarize(
            [
                {"correct": True, "confidence_inversion": False, "retracts": 1, "winner": {"from": "a"}, "first_claim_from": "a"},
                {"correct": False, "confidence_inversion": True, "retracts": 0, "winner": {"from": "a"}, "first_claim_from": "a"},
            ]
        )
        self.assertEqual(summary["trials"], 2)
        self.assertEqual(summary["accuracy"], 0.5)


if __name__ == "__main__":
    unittest.main()
