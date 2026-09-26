import unittest

from outbox.protocol import Message, validate_message


class ProtocolTests(unittest.TestCase):
    def test_claim_requires_confidence_and_answer(self):
        msg = Message(
            type="claim",
            from_agent="claimant-a",
            to=["ledger"],
            task_id="T",
            env_seq=1,
            created_at_tick=0,
        )
        errors = validate_message(msg.to_dict())
        self.assertTrue(any(e.startswith("claim_needs") for e in errors))

    def test_roundtrip_from_field(self):
        raw = {
            "id": "x",
            "from": "scout",
            "to": ["claimant-a"],
            "type": "env_event",
            "task_id": "T",
            "env_seq": 1,
            "created_at_tick": 0,
            "payload": {"fact": "hi"},
        }
        self.assertEqual(validate_message(raw), [])
        msg = Message.from_dict(raw)
        self.assertEqual(msg.from_agent, "scout")
        self.assertEqual(msg.to_dict()["from"], "scout")


if __name__ == "__main__":
    unittest.main()
