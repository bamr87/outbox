import json
import tempfile
import unittest
from pathlib import Path

from outbox.mailbox import Mailbox, Relay
from outbox.protocol import Message


class MailboxTests(unittest.TestCase):
    def test_outbox_then_relay_does_not_touch_peer_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = Mailbox(root, "claimant-a")
            b = Mailbox(root, "claimant-b")
            Mailbox(root, "scout")
            before = json.dumps(b.load_state(), sort_keys=True)
            msg = Message(
                type="claim",
                from_agent="claimant-a",
                to=["claimant-b", "ledger"],
                task_id="T",
                env_seq=1,
                created_at_tick=3,
                confidence=0.8,
                payload={"answer": "green"},
            )
            a.write_outbox(msg)
            delivered = Relay(root, ["scout", "claimant-a", "claimant-b"]).deliver_new()
            self.assertEqual(delivered, [msg.id])
            self.assertTrue(list(b.inbox.glob("*.json")))
            self.assertEqual(json.dumps(b.load_state(), sort_keys=True), before)
            self.assertTrue((root / "ledger.jsonl").exists())

    def test_invalid_message_goes_dead_letter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = Mailbox(root, "claimant-a")
            Mailbox(root, "claimant-b")
            Mailbox(root, "scout")
            bad = a.outbox / "000001_claim_bad.json"
            bad.write_text(json.dumps({"id": "bad", "type": "claim", "from": "claimant-a"}), encoding="utf-8")
            Relay(root, ["scout", "claimant-a", "claimant-b"]).deliver_new()
            self.assertTrue((root / "dead_letter.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
